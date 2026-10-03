import base64
import subprocess

from meeting_copilot import clipboard


class _Runs:
    """Stands in for subprocess.run: records each tool tried, fails the ones listed."""

    def __init__(self, fail=(), timeout=()):
        self.fail, self.timeout = set(fail), set(timeout)
        self.calls = []

    def __call__(self, cmd, input=None, stdout=None, stderr=None, timeout=None):
        self.calls.append((cmd[0], input))
        assert stdout is subprocess.DEVNULL and stderr is subprocess.DEVNULL
        if cmd[0] in self.timeout:
            raise subprocess.TimeoutExpired(cmd, timeout)
        return subprocess.CompletedProcess(cmd, 1 if cmd[0] in self.fail else 0)


def _setup(monkeypatch, installed, runs):
    monkeypatch.setattr(clipboard.shutil, "which",
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
