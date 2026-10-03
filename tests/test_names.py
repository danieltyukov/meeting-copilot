"""The naming rule, held to the fixture the extension's names.js also reads."""

import json
from pathlib import Path

import pytest

from meeting_copilot.names import detect_name, same_name

CASES = json.loads((Path(__file__).parent / "name_cases.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("text,expected", CASES["positive"])
def test_detects_the_name(text, expected):
    assert detect_name(text) == expected


@pytest.mark.parametrize("text", CASES["negative"])
def test_finds_no_name(text):
    assert detect_name(text) is None


def test_cues_are_tried_in_order_not_by_position():
    # "this is Tom" comes first in the line, but "my name is" is the stronger cue.
    assert detect_name("Hi, this is Tom here. Well, my name is Thomas.") == "Thomas"


def test_decomposed_accents_read_as_one_letter():
    assert detect_name("Hi, I'm Zoe\u0308.") == "Zoë"


def test_contractions_and_curly_apostrophes():
    assert detect_name("I’m Sarah.") == "Sarah"
    assert detect_name("I'm here.") is None
    assert detect_name("I’m here.") is None
    assert detect_name("Yeah, I’m Sarah’s manager.") is None


def test_this_is_with_a_follower_anywhere():
    assert detect_name("Okay so this is Marcus speaking for the team.") == "Marcus"
    assert detect_name("Good evening folks, this is Ana.") == "Ana"


def test_stopped_second_word_is_dropped():
    assert detect_name("My name is Sarah Monday.") == "Sarah"


def test_titles():
    assert detect_name("I'm Professor Lee.") == "Professor Lee"
    assert detect_name("I'm Ms Okafor.") == "Ms Okafor"
    assert detect_name("I'm Doctor and engineer.") is None   # a bare title is not a name


def test_same_name():
    assert same_name("Daniel", "Daniel Tyukov")
    assert same_name("daniel tyukov", "Daniel")
    assert same_name("Dr. Patel", "patel")
    assert not same_name("Dan", "Daniel")
    assert not same_name("Sarah Connor", "Sarah Smith")
    assert not same_name("", "Sarah")
