"""Single keypresses from the terminal, on Linux, macOS and Windows.

A POSIX terminal is put in cbreak mode and sends the arrow and page keys as
escape sequences. The Windows console hands keys over one at a time through
``msvcrt``, with those keys as a two-character scan code. Both come out the
same: one character per key, or a name from ``NAMED``.
"""

from __future__ import annotations

import sys
import threading
import time
from typing import Iterator

NAMED = ("up", "down", "pgup", "pgdn")

# What follows Esc on a POSIX terminal. Page keys end in "~", read separately.
ESCAPES = {"[A": "up", "[B": "down", "[5": "pgup", "[6": "pgdn"}

# What follows the "\x00" or "\xe0" lead-in from msvcrt.getwch().
SCAN_CODES = {"H": "up", "P": "down", "I": "pgup", "Q": "pgdn"}
SCAN_LEADS = ("\x00", "\xe0")


def read_keys(stop: threading.Event) -> Iterator[str]:
    """Yield keypresses until ``stop`` is set. The terminal is restored when
    the loop ends, so set ``stop`` from the consumer rather than abandoning it."""
    if sys.platform == "win32":
        yield from _windows_keys(stop)
    else:
        yield from _posix_keys(stop)


def _posix_keys(stop: threading.Event) -> Iterator[str]:
    import select
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while not stop.is_set():
            ch = sys.stdin.read(1)
            if not ch:
                continue
            if ch == "\x1b" and select.select([sys.stdin], [], [], 0.04)[0]:
                seq = sys.stdin.read(2)                # e.g. "[A", "[B", "[5"
                if seq in ("[5", "[6") and select.select([sys.stdin], [], [], 0.01)[0]:
                    sys.stdin.read(1)                  # consume trailing "~"
                if seq in ESCAPES:
                    yield ESCAPES[seq]
                continue
            yield ch                                   # a lone Esc included
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _windows_keys(stop: threading.Event) -> Iterator[str]:
    import msvcrt

    while not stop.is_set():
        # Poll so a quit from another thread is noticed; getwch() would block.
        if not msvcrt.kbhit():
            time.sleep(0.02)
            continue
        ch = msvcrt.getwch()
        if ch in SCAN_LEADS:
            name = SCAN_CODES.get(msvcrt.getwch())
            if name:
                yield name
            continue
        yield ch
