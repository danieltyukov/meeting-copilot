"""Who is in the meeting, and how that names the voices.

Introductions are one source of names; the meeting itself is the other. In
person that is the calendar invite (or names given with ``--people``). The
roster is the list of the other people, and it is used three ways: an intro
"I'm Sarah" is matched to "Sarah Chen" on it, the last voice left unnamed gets
the last name left over (elimination), and it goes into the drafting prompt and
the saved participants list.

The rules are shared with ``extension/names.js`` and both are held to
``tests/roster_cases.json``; see the "Names from the meeting" section of
docs/superpowers/specs/2026-10-03-speaker-names-design.md.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path

ROLES = ("host", "co-host", "guest", "external", "presenting", "organizer", "organiser",
         "presenter", "me", "you")
# Professional suffixes, dropped from the end of a name the same way as a role.
DEGREES = ("phd", "md", "mba", "esq")


def _trailing(words, dot: bool = False) -> re.Pattern:
    alt = "(?:" + "|".join(re.escape(w) for w in words) + ")" + (r"\.?" if dot else "")
    return re.compile(rf"\s*(?:\(\s*{alt}\s*\)|,\s*{alt})$", re.IGNORECASE)


_TRAILING_ROLE = _trailing(ROLES)
_TRAILING_DEGREE = _trailing(DEGREES, dot=True)   # "Esq." as well as "Esq"
_SELF_MARK = re.compile(r"\(\s*(?:you|me)\s*\)", re.IGNORECASE)
# The meeting apps' own labels, which a scrape of the people list picks up too.
_UI_TEXT = re.compile(r"(?:add people|participants|people|in this meeting|contributors)"
                      r"(?:\s*\(\d+\))?", re.IGNORECASE)
NAME_MAX = 60
# Name suffixes that must not be read as a first name in "Last, First".
_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", *DEGREES}


def name_key(name: str) -> str:
    """Compare form of a name: lower case, no diacritics, single spaces."""
    decomposed = unicodedata.normalize("NFD", name or "")
    bare = "".join(c for c in decomposed if not unicodedata.category(c).startswith("M"))
    return " ".join(bare.lower().split())   # lower(), not casefold(): as names.js does


def clean_roster_name(raw: str | None) -> str | None:
    """A display name as the meeting shows it, tidied, or None when it is not
    someone else's name (you, a role word, the app's own labels, an icon
    name, an address, a phone number, or something far too long)."""
    lines = (raw or "").splitlines()
    name = " ".join(lines[0].split()) if lines else ""
    if _SELF_MARK.search(name):
        return None
    # One role, then one degree: "Anil Patel, MD (Host)" is "Anil Patel".
    name = _TRAILING_DEGREE.sub("", _TRAILING_ROLE.sub("", name).strip()).strip()
    low = name.lower()
    if (not name or low in ROLES or low.rstrip(".") in DEGREES or low == "presentation"
            or "@" in name or "_" in name
            or _UI_TEXT.fullmatch(name) or len(name) > NAME_MAX
            or not any(c.isalpha() for c in name)):
        return None
    return name


def roster_others(my_name: str | None, roster) -> list[str]:
    """The roster cleaned, without duplicates (the first spelling wins) and
    without me: a full match of my name, or its first word when my name is
    a single word."""
    mine = name_key(my_name or "").split()
    out: list[str] = []
    seen: set[str] = set()
    for raw in roster:
        name = clean_roster_name(raw)
        if name is None:
            continue
        key = name_key(name)
        if key in seen:
            continue
        seen.add(key)
        words = key.split()
        if mine and (words == mine or (len(mine) == 1 and words[:1] == mine)):
            continue
        out.append(name)
    return out


def match_roster(name: str, roster) -> str:
    """The roster entry a name refers to: a full-name match, or a one-word
    first name that matches exactly one entry. Otherwise the name as given."""
    key = name_key(name)
    if not key:
        return name
    for entry in roster:
        if name_key(entry) == key:
            return entry
    if " " not in key:
        hits = [e for e in roster if name_key(e).split()[:1] == [key]]
        if len(hits) == 1:
            return hits[0]
    return name


def unused_roster(names, roster) -> list[str]:
    """Roster entries that none of ``names`` refers to."""
    used = {match_roster(n, roster) for n in names if n}
    return [r for r in roster if r not in used]


def eliminate(voices, names: dict[str, str], roster) -> dict[str, str]:
    """Name the one voice left by the one name left.

    Only when there are as many other voices as people on the roster, exactly
    one voice is unnamed and exactly one roster name is unused; it never
    guesses between two. Returns the new assignments (empty or one).
    """
    voices = list(dict.fromkeys(voices))
    roster = list(roster)
    if not roster or len(voices) != len(roster):
        return {}
    unnamed = [v for v in voices if not names.get(v)]
    unused = unused_roster([names[v] for v in voices if names.get(v)], roster)
    if len(unnamed) == 1 and len(unused) == 1:
        return {unnamed[0]: unused[0]}
    return {}


# -- where the roster comes from ----------------------------------------------
def parse_people(text: str | None) -> list[str]:
    """``--people "Sarah Chen, Marcus Lee"``: names split on commas or semicolons."""
    return [p.strip() for p in re.split(r"[,;\n]", text or "") if p.strip()]


def _unfold(text: str) -> list[str]:
    """iCalendar content lines: a line that starts with a space or a tab
    continues the one before it (RFC 5545, section 3.1)."""
    lines: list[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if line[:1] in (" ", "\t") and lines:
            lines[-1] += line[1:]
        else:
            lines.append(line)
    return lines


def _split_property(line: str) -> list[str]:
    """``NAME;PARAM=...;PARAM=...:VALUE`` split into its parts, the value last.
    Semicolons and colons inside double quotes or after a backslash do not split."""
    parts, cur, quoted, i = [], "", False, 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur += line[i:i + 2]
            i += 2
            continue
        if c == '"':
            quoted = not quoted
        elif c in ";:" and not quoted:
            parts.append(cur)
            if c == ":":
                return parts + [line[i + 1:]]
            cur, i = "", i + 1
            continue
        cur += c
        i += 1
    return parts + [cur]


def _params(parts: list[str]) -> dict[str, str]:
    """``NAME=VALUE`` parameters, with a quoted value unwrapped."""
    out: dict[str, str] = {}
    for part in parts:
        key, eq, value = part.partition("=")
        if not eq:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] == '"':
            value = value[1:-1]
        out[key.strip().upper()] = value
    return out


def _unescape(value: str) -> str:
    return re.sub(r"\\(.)", lambda m: " " if m.group(1) in "nN" else m.group(1), value)


def _first_last(name: str) -> str:
    """Directory-style "Chen, Sarah" as "Sarah Chen". Only one word each side,
    and never when the second part is a role or a suffix ("Patel, Organizer")."""
    parts = [p.strip() for p in name.split(",")]
    if (len(parts) == 2 and all(len(p.split()) == 1 and any(c.isalpha() for c in p)
                                for p in parts)
            and parts[1].lower() not in ROLES and parts[1].lower().strip(".") not in _SUFFIXES):
        return f"{parts[1]} {parts[0]}"
    return name


def parse_ics(text: str) -> list[str]:
    """The ``CN=`` names of every ATTENDEE and ORGANIZER in a calendar file.

    Rooms and resources are skipped, and so is anyone who declined, since
    neither will be speaking. Entries without a CN are skipped too.
    """
    names: list[str] = []
    for line in _unfold(text):
        parts = _split_property(line)
        if parts[0].strip().upper() not in ("ATTENDEE", "ORGANIZER") or len(parts) < 2:
            continue
        params = _params(parts[1:-1])        # the last part is the value (mailto:...)
        if params.get("CUTYPE", "").upper() in ("ROOM", "RESOURCE"):
            continue
        if params.get("PARTSTAT", "").upper() == "DECLINED":
            continue
        cn = _unescape(params.get("CN", "")).strip()
        if cn:
            names.append(_first_last(cn))
    return names


def ics_files(directory) -> list[Path]:
    try:
        return sorted(p for p in Path(directory).iterdir()
                      if p.suffix.lower() == ".ics" and p.is_file())
    except OSError:
        return []


def find_invite(directory) -> Path | None:
    """The calendar invite at the root of the launch directory, when there is
    exactly one ``*.ics`` there; None when there are none or several."""
    found = ics_files(directory)
    return found[0] if len(found) == 1 else None
