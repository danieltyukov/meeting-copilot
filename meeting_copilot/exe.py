"""Find the programs Sparky runs: ffmpeg, git, claude and the clipboard tools.

Sparky runs inside the project being discussed, which may be someone else's
repository. On Windows, ``shutil.which`` and a bare program name handed to
``subprocess`` both look in the current directory before PATH, so a planted
``ffmpeg.exe`` or ``claude.bat`` there would run in place of the real one.
This looks in the absolute PATH entries only, and callers run the full path it
returns.
"""

from __future__ import annotations

import os
import sys


def find_executable(name: str) -> str | None:
    """The full path of ``name`` on PATH, never from the current directory."""
    if sys.platform == "win32":
        # .exe only: that is what CreateProcess runs directly, with no cmd.exe.
        names = [name if name.lower().endswith(".exe") else name + ".exe"]
    else:
        names = [name]
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        folder = folder.strip('"')             # Windows PATH entries may be quoted
        if not os.path.isabs(folder):
            continue                           # "", "." and other relative entries
        for candidate in names:
            path = os.path.join(folder, candidate)
            if os.path.isfile(path) and os.access(path, os.X_OK):
                return path
    return None
