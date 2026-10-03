import base64
import subprocess
import sys

import pytest

from meeting_copilot import clipboard


class _Runs:
    """Stands in for subprocess.run: records each tool tried, fails the ones listed."""

    def __init__(self, fail=(), timeout=()):
        self.fail, self.timeout = set(fail), set(timeout)
        self.calls = []

    def __call__(self, cmd, input=None, stdout=None, stderr=None, timeout=None):
        name = cmd[0].rsplit("/", 1)[-1]          # run by the full path
        self.calls.append((name, input))
        assert stdout is subprocess.DEVNULL and stderr is subprocess.DEVNULL
        if name in self.timeout:
            raise subprocess.TimeoutExpired(cmd, timeout)
        return subprocess.CompletedProcess(cmd, 1 if name in self.fail else 0)


def _setup(monkeypatch, installed, runs, native=None):
    monkeypatch.setattr(clipboard, "NATIVE", native)
    monkeypatch.setattr(clipboard, "find_executable",
                        lambda name: f"/usr/bin/{name}" if name in installed else None)
    monkeypatch.setattr(clipboard.subprocess, "run", runs)


def test_first_working_tool_wins(monkeypatch):
    runs = _Runs()
    _setup(monkeypatch, {"wl-copy", "xclip"}, runs)
    assert clipboard.copy("draft") == "wl-copy"
    assert runs.calls == [("wl-copy", b"draft")]


def test_falls_through_the_chain_in_order(monkeypatch):
    runs = _Runs(fail={"wl-copy"}, timeout={"xclip"})
    _setup(monkeypatch, {"wl-copy", "xclip", "xsel", "pbcopy"}, runs)
    assert clipboard.copy("draft") == "xsel"
    assert [c[0] for c in runs.calls] == ["wl-copy", "xclip", "xsel"]


def test_skips_tools_that_are_not_installed(monkeypatch):
    runs = _Runs()
    _setup(monkeypatch, {"pbcopy"}, runs)
    assert clipboard.copy("draft") == "pbcopy"
    assert [c[0] for c in runs.calls] == ["pbcopy"]


def test_osc52_when_nothing_works(monkeypatch):
    runs = _Runs(fail={"wl-copy", "xclip", "xsel", "pbcopy"})
    _setup(monkeypatch, {"wl-copy", "xclip", "xsel", "pbcopy"}, runs)
    written = []
    assert clipboard.copy("Grüße, Sarah", write=written.append) == "osc52"
    assert len(runs.calls) == 4
    seq = written[0]
    assert seq.startswith("\x1b]52;c;") and seq.endswith("\x07")
    assert base64.b64decode(seq[len("\x1b]52;c;"):-1]).decode("utf-8") == "Grüße, Sarah"


def test_osc52_over_ssh_with_no_tools(monkeypatch):
    runs = _Runs()
    _setup(monkeypatch, set(), runs)
    written = []
    assert clipboard.copy("x", write=written.append) == "osc52"
    assert runs.calls == [] and written == [clipboard.osc52("x")]


def test_native_clipboard_comes_first(monkeypatch):
    runs, copied = _Runs(), []
    _setup(monkeypatch, {"pbcopy"}, runs,
           native=("win32", lambda text: copied.append(text) or True))
    assert clipboard.copy("Grüße, Sarah") == "win32"
    assert copied == ["Grüße, Sarah"] and runs.calls == []


def test_native_failure_falls_through(monkeypatch):
    def broken(text):
        raise OSError("clipboard is locked")

    runs = _Runs()
    _setup(monkeypatch, set(), runs, native=("win32", broken))
    written = []
    assert clipboard.copy("x", write=written.append) == "osc52"

    _setup(monkeypatch, set(), runs, native=("win32", lambda text: False))
    assert clipboard.copy("x", write=written.append) == "osc52"


def test_native_is_only_set_on_windows():
    assert (clipboard.NATIVE is not None) == (clipboard.sys.platform == "win32")


@pytest.mark.skipif(sys.platform != "win32", reason="the Win32 clipboard")
def test_win32_round_trip():
    import ctypes
    from ctypes import wintypes

    text = "Grüße\nSarah \U0001d538"          # a non-BMP letter: a UTF-16 surrogate pair
    if not clipboard._win32_copy(text):
        pytest.skip("no clipboard in this session")
    user32, kernel32 = ctypes.WinDLL("user32"), ctypes.WinDLL("kernel32")
    user32.GetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    assert user32.OpenClipboard(None)
    try:
        handle = user32.GetClipboardData(13)
        back = ctypes.wstring_at(kernel32.GlobalLock(handle))
        kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()
    assert back == "Grüße\r\nSarah \U0001d538"
