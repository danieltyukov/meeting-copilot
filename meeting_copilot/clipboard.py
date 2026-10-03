"""Copy a draft to the clipboard.

On Windows the clipboard is set through the Win32 API. Elsewhere the desktop
clipboard tools are tried in order (Wayland, X11 twice, macOS). Both fall back
to an OSC 52 escape, which asks the terminal itself to set the clipboard. That
last one is what makes ``y`` work over SSH, where none of the tools can reach
the machine you are sitting at.
"""

from __future__ import annotations

import base64
import subprocess
import sys
import time
from typing import Callable

from .exe import find_executable

TOOLS = [
    ("wl-copy", ["wl-copy"]),
    ("xclip", ["xclip", "-selection", "clipboard"]),
    ("xsel", ["xsel", "--clipboard", "--input"]),
    ("pbcopy", ["pbcopy"]),
]


def _win32_copy(text: str) -> bool:
    """Put ``text`` on the Windows clipboard as CF_UNICODETEXT."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    # Handles are pointer-sized: without these, 64-bit handles get truncated.
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]

    data = ctypes.create_unicode_buffer(text.replace("\r\n", "\n").replace("\n", "\r\n"))
    size = ctypes.sizeof(data)                 # UTF-16 with its terminating NUL
    # Another program may hold the clipboard for a moment; wait it out briefly.
    for _ in range(10):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.05)
    else:
        return False
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(0x0002, size)      # GMEM_MOVEABLE
        if not handle:
            return False
        ptr = kernel32.GlobalLock(handle)
        if not ptr:
            kernel32.GlobalFree(handle)
            return False
        ctypes.memmove(ptr, data, size)
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(13, handle):      # CF_UNICODETEXT
            kernel32.GlobalFree(handle)                  # still ours on failure
            return False
        return True                                      # the system owns it now
    finally:
        user32.CloseClipboard()


# The native clipboard, where there is one to call directly.
NATIVE = ("win32", _win32_copy) if sys.platform == "win32" else None


def osc52(text: str) -> str:
    """The escape sequence that puts ``text`` on the terminal's clipboard."""
    return "\x1b]52;c;" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "\x07"


def _write_stdout(seq: str) -> None:
    sys.stdout.write(seq)
    sys.stdout.flush()


def copy(text: str, write: Callable[[str], None] | None = None) -> str:
    """Copy ``text`` and return how: a tool name, or ``"osc52"`` when no tool
    worked and the escape was written with ``write`` (stdout by default)."""
    if NATIVE is not None:
        name, native = NATIVE
        try:
            if native(text):
                return name
        except Exception:      # any failure here leaves the tools and OSC 52
            pass
    for name, cmd in TOOLS:
        exe = find_executable(cmd[0])
        if exe is None:
            continue
        try:
            # The tools fork a child that keeps serving the selection, so their
            # output must not be a pipe we wait on.
            proc = subprocess.run([exe, *cmd[1:]], input=text.encode("utf-8"), stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=2)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            return name
    (write or _write_stdout)(osc52(text))
    return "osc52"
