import sys
import threading
import types

from meeting_copilot import keys


def _fake_msvcrt(monkeypatch, typed: str, stop: threading.Event):
    """A console that has ``typed`` waiting, then sets ``stop`` once it runs dry."""
    pending = list(typed)

    def kbhit():
        if not pending:
            stop.set()
        return bool(pending)

    fake = types.SimpleNamespace(kbhit=kbhit, getwch=lambda: pending.pop(0))
    monkeypatch.setitem(sys.modules, "msvcrt", fake)


def test_windows_scan_codes_become_named_keys(monkeypatch):
    stop = threading.Event()
    # h, Up, Down, PgUp, PgDn, F1 (no meaning here, dropped), Esc, Enter
    _fake_msvcrt(monkeypatch, "h\xe0H\xe0P\x00I\x00Q\x00;\x1b\r", stop)
    assert list(keys._windows_keys(stop)) == ["h", "up", "down", "pgup", "pgdn", "\x1b", "\r"]


def test_windows_reader_stops_when_asked(monkeypatch):
    stop = threading.Event()
    stop.set()
    _fake_msvcrt(monkeypatch, "q", stop)
    assert list(keys._windows_keys(stop)) == []


def test_named_keys_cover_both_tables():
    assert set(keys.ESCAPES.values()) == set(keys.NAMED) == set(keys.SCAN_CODES.values())
