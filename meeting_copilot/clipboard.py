"""Copy a draft to the clipboard.

Tries the desktop clipboard tools in order (Wayland, X11 twice, macOS) and
falls back to an OSC 52 escape, which asks the terminal itself to set the
clipboard. That last one is what makes ``y`` work over SSH, where none of the
tools can reach the machine you are sitting at.
"""

from __future__ import annotations

import base64
import shutil
import subprocess
import sys
from typing import Callable

TOOLS = [
    ("wl-copy", ["wl-copy"]),
    ("xclip", ["xclip", "-selection", "clipboard"]),
    ("xsel", ["xsel", "--clipboard", "--input"]),
    ("pbcopy", ["pbcopy"]),
]


def osc52(text: str) -> str:
    """The escape sequence that puts ``text`` on the terminal's clipboard."""
    return "\x1b]52;c;" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "\x07"


def _write_stdout(seq: str) -> None:
    sys.stdout.write(seq)
    sys.stdout.flush()


def copy(text: str, write: Callable[[str], None] | None = None) -> str:
    """Copy ``text`` and return how: a tool name, or ``"osc52"`` when no tool
    worked and the escape was written with ``write`` (stdout by default)."""
    for name, cmd in TOOLS:
        if shutil.which(cmd[0]) is None:
            continue
        try:
            # The tools fork a child that keeps serving the selection, so their
            # output must not be a pipe we wait on.
            proc = subprocess.run(cmd, input=text.encode("utf-8"), stdout=subprocess.DEVNULL,
                                  stderr=subprocess.DEVNULL, timeout=2)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0:
            return name
    (write or _write_stdout)(osc52(text))
    return "osc52"
