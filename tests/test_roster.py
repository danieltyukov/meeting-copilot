"""The roster rules, held to the fixture the extension's names.js also reads,
plus where the terminal app gets a roster from: --people and calendar invites."""

import json
from pathlib import Path

import pytest

from meeting_copilot.roster import (clean_roster_name, eliminate, find_invite, match_roster,
                                    parse_ics, parse_people, roster_others)

CASES = json.loads((Path(__file__).parent / "roster_cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("raw,expected", CASES["clean"])
def test_clean_roster_name(raw, expected):
    assert clean_roster_name(raw) == expected


@pytest.mark.parametrize("name,roster,expected", CASES["match"])
def test_match_roster(name, roster, expected):
    assert match_roster(name, roster) == expected


@pytest.mark.parametrize("me,roster,expected", CASES["others"])
def test_roster_others(me, roster, expected):
    assert roster_others(me, roster) == expected


@pytest.mark.parametrize("case", CASES["eliminate"], ids=[c["why"] for c in CASES["eliminate"]])
def test_eliminate(case):
    assert eliminate(case["voices"], case["names"], case["roster"]) == case["expect"]


def test_match_needs_a_single_first_name_for_a_partial_match():
    assert match_roster("Sarah Smith", ["Sarah Chen"]) == "Sarah Smith"
    assert match_roster("", ["Sarah Chen"]) == ""


# -- --people -----------------------------------------------------------------------
def test_parse_people():
    assert parse_people("Sarah Chen, Marcus Lee") == ["Sarah Chen", "Marcus Lee"]
    assert parse_people(" Sarah Chen ;Marcus Lee,, ") == ["Sarah Chen", "Marcus Lee"]
    assert parse_people("") == [] and parse_people(None) == []


# -- calendar invites ---------------------------------------------------------------
def _ics(*props):
    return "\r\n".join(["BEGIN:VCALENDAR", "BEGIN:VEVENT", *props, "END:VEVENT",
                        "END:VCALENDAR", ""])


def test_ics_attendees_and_organizer():
    text = _ics("ORGANIZER;CN=Daniel Tyukov:mailto:daniel@example.com",
                "ATTENDEE;ROLE=REQ-PARTICIPANT;CN=Sarah Chen:mailto:sarah@example.com",
                "ATTENDEE;CN=Marcus Lee;RSVP=TRUE:mailto:marcus@example.com")
    assert parse_ics(text) == ["Daniel Tyukov", "Sarah Chen", "Marcus Lee"]


def test_ics_line_unfolding():
    text = _ics("ATTENDEE;ROLE=REQ-PARTICIPANT;PARTSTAT=ACCEPTED;CN=Priya",
                "  Raman:mailto:priya@example.com",          # folded: CRLF + one space
                "ATTENDEE;CN=Wei Ch",
                "\ten:mailto:wei@example.com")               # folded with a tab
    assert parse_ics(text) == ["Priya Raman", "Wei Chen"]


def test_ics_quoted_values_may_hold_separators():
    text = _ics('ATTENDEE;DELEGATED-FROM="mailto:boss@example.com";CN="Sarah Chen":'
                "mailto:sarah@example.com",
                'ATTENDEE;CN="Lee; Marcus: PM":mailto:marcus@example.com')
    assert parse_ics(text) == ["Sarah Chen", "Lee; Marcus: PM"]


def test_ics_escaped_commas_and_directory_style_names():
    text = _ics("ATTENDEE;CN=Chen\\, Sarah:mailto:sarah@example.com",
                'ATTENDEE;CN="Lee, Marcus":mailto:marcus@example.com',
                "ATTENDEE;CN=Patel\\, Organizer:mailto:anil@example.com",
                "ATTENDEE;CN=Okafor\\, Jr.:mailto:ada@example.com",
                "ATTENDEE;CN=Van der Berg\\, Anna:mailto:anna@example.com")
    assert parse_ics(text) == ["Sarah Chen", "Marcus Lee", "Patel, Organizer", "Okafor, Jr.",
                               "Van der Berg, Anna"]
    # cleaning then drops the trailing role
    assert roster_others("", parse_ics(text))[2] == "Patel"


def test_ics_skips_entries_without_a_name_rooms_and_declines():
    text = _ics("ATTENDEE;RSVP=TRUE:mailto:nobody@example.com",
                "ATTENDEE;CN=:mailto:empty@example.com",
                "ATTENDEE;CUTYPE=ROOM;CN=Room 4.12:mailto:room@example.com",
                "ATTENDEE;CUTYPE=RESOURCE;CN=Projector:mailto:p@example.com",
                "ATTENDEE;PARTSTAT=DECLINED;CN=Tom Becker:mailto:tom@example.com",
                "ATTENDEE;CN=Sarah Chen:mailto:sarah@example.com",
                "DESCRIPTION:ATTENDEE;CN=Not Me")
    assert parse_ics(text) == ["Sarah Chen"]


def test_ics_with_bare_newlines_and_lowercase_names():
    text = "BEGIN:VEVENT\nattendee;cn=Sarah Chen:mailto:s@example.com\nEND:VEVENT\n"
    assert parse_ics(text) == ["Sarah Chen"]


def test_find_invite(tmp_path):
    assert find_invite(tmp_path) is None                     # none
    (tmp_path / "notes.md").write_text("x")
    (tmp_path / "Team Sync.ICS").write_text("x")
    assert find_invite(tmp_path) == tmp_path / "Team Sync.ICS"
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "other.ics").write_text("x")         # only the root counts
    assert find_invite(tmp_path) == tmp_path / "Team Sync.ICS"
    (tmp_path / "second.ics").write_text("x")
    assert find_invite(tmp_path) is None                     # several: no guessing
    assert find_invite(tmp_path / "missing") is None


def test_clean_strips_a_role_then_a_degree():
    assert clean_roster_name("Anil Patel, MD (Host)") == "Anil Patel"
    assert clean_roster_name("Sarah Chen (mba)") == "Sarah Chen"
    assert clean_roster_name("People Ops") == "People Ops"     # only the bare UI label
    assert clean_roster_name("Sarah_Chen") is None
    assert clean_roster_name("x" * 60) == "x" * 60
    assert clean_roster_name("x" * 61) is None
    assert clean_roster_name("x" * 58 + " (Host)") == "x" * 58   # measured after the strip
    assert clean_roster_name("Ada Okafor (PhD.)") == "Ada Okafor"
    assert clean_roster_name("MBA.") is None                     # a bare degree is no one


def test_people_flag_with_a_degree_does_not_invent_a_person():
    assert roster_others("", parse_people("Anil Patel, PhD; Sarah Chen")) == [
        "Anil Patel", "Sarah Chen"]
