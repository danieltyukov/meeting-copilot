from datetime import datetime

from meeting_copilot.session import Session, State, fmt_clock


def test_recording_gates_utterances():
    s = Session()
    assert s.add_utterance(1.0, "A", "before start") is None  # idle -> ignored
    s.start()
    assert s.is_recording
    assert s.add_utterance(1.0, "A", "hello") is not None
    assert s.add_utterance(2.0, "A", "   ") is None  # empty ignored
    assert len(s.utterances) == 1


def test_latest_question_prefers_other_speaker():
    s = Session()
    s.start()
    s.add_utterance(1.0, "A", "I am the candidate")
    s.add_utterance(2.0, "B", "Why do you want this job?")
    s.add_utterance(3.0, "A", "Because…")
    s.set_me("A")
    q = s.latest_question()
    assert q.text == "Why do you want this job?"  # most recent non-me


def test_latest_question_fallback_without_me_label():
    s = Session()
    s.start()
    s.add_utterance(1.0, "A", "first")
    s.add_utterance(2.0, "B", "second")
    assert s.latest_question().text == "second"


def test_speaker_naming():
    s = Session()
    s.set_me("A")
    assert s.speaker_name("A") == "Me"
    assert s.speaker_name("B") == "Speaker B"
    s.set_me(None)
    assert s.speaker_name("A") == "Speaker A"


def test_export_markdown_and_file(tmp_path):
    s = Session()
    s.start(now=datetime(2026, 6, 14, 10, 0, 0))
    s.add_utterance(0.0, "B", "Tell me about this project.")
    s.add_utterance(5.0, "A", "It is a meeting copilot.")
    s.add_assist(6.0, "Tell me about this project.", "I built a meeting copilot…")
    s.set_me("A")
    s.end(now=datetime(2026, 6, 14, 10, 1, 30))

    md = s.export_markdown(tmp_path)
    assert "# Meeting transcript" in md
    assert "Tell me about this project." in md
    assert "Copilot assists" in md
    assert "Duration:** 01:30" in md

    dest = s.write_export(tmp_path)
    assert dest.exists()
    assert dest.name.startswith("meeting-")
    assert dest.read_text().strip() == md.strip()


def test_fmt_clock():
    assert fmt_clock(0) == "00:00"
    assert fmt_clock(75) == "01:15"


# -- picking the question worth answering --------------------------------------
def _convo():
    s = Session()
    s.start()
    s.set_me("A")
    return s


def test_latest_question_prefers_a_question_over_a_backchannel():
    s = _convo()
    s.add_utterance(1.0, "B", "How do you handle schema migrations?")
    s.add_utterance(5.0, "A", "Expand and contract, with a backfill.")
    s.add_utterance(9.0, "B", "Right, yes.")
    assert s.latest_question().text == "How do you handle schema migrations?"


def test_latest_question_joins_a_run_from_the_same_speaker():
    s = _convo()
    s.add_utterance(1.0, "B", "So, one more thing.")
    s.add_utterance(2.0, "B", "How did you test the migration path?")
    q = s.latest_question()
    assert q.text == "So, one more thing. How did you test the migration path?"


def test_latest_question_falls_back_to_the_last_far_end_line():
    s = _convo()
    s.add_utterance(1.0, "B", "Tell me about the caching layer")   # no question mark
    s.add_utterance(5.0, "A", "Sure.")
    assert s.latest_question().text == "Tell me about the caching layer"


def test_latest_question_does_not_dig_up_a_stale_question():
    s = _convo()
    s.add_utterance(1.0, "B", "Why Rust?")
    for i in range(12):                              # a long stretch with no question
        s.add_utterance(10.0 + i, "B", f"statement number {i}")
    assert s.latest_question().text != "Why Rust?"


def test_last_line_is_the_newest_utterance_from_anyone():
    s = _convo()
    assert s.last_line() is None
    s.add_utterance(1.0, "B", "Why Rust?")
    s.add_utterance(2.0, "A", "Because of the borrow checker.")
    assert s.last_line().text == "Because of the borrow checker."


def test_assists_record_their_kind_and_export_labels_them(tmp_path):
    s = Session()
    s.start(now=datetime(2026, 6, 14, 10, 0, 0))
    s.add_utterance(0.0, "B", "We shipped v2 last week.")
    s.add_assist(1.0, "We shipped v2 last week.", "- I can walk through what changed in v2.",
                 kind="points")
    s.add_assist(2.0, "Why v2?", "Because v1 could not scale.")
    assert [a.kind for a in s.assists] == ["points", "answer"]
    md = s.export_markdown(tmp_path)
    assert "Talking points" in md
    assert "Drafted answer" in md


# -- one keypress: answer or talking points, decided from the transcript -------
def test_decide_help_answers_a_question_put_to_me():
    s = _convo()
    s.add_utterance(1.0, "B", "How do you handle schema migrations?")
    assert s.decide_help() == ("answer", "How do you handle schema migrations?")


def test_decide_help_continues_after_i_have_answered():
    s = _convo()
    s.add_utterance(1.0, "B", "How do you handle schema migrations?")
    s.add_utterance(5.0, "A", "Expand and contract, with a backfill.")
    assert s.decide_help() == ("points", "Expand and contract, with a backfill.")


def test_decide_help_treats_a_backchannel_as_a_cue_to_continue():
    s = _convo()
    s.add_utterance(1.0, "B", "How do you handle schema migrations?")
    s.add_utterance(5.0, "A", "Expand and contract, with a backfill.")
    s.add_utterance(9.0, "B", "Right, yes.")
    assert s.decide_help() == ("points", "Right, yes.")


def test_decide_help_answers_a_question_that_came_with_a_lead_in():
    s = _convo()
    s.add_utterance(1.0, "A", "That is the gist of it.")
    s.add_utterance(2.0, "B", "So, one more thing.")
    s.add_utterance(3.0, "B", "How did you test the migration path?")
    assert s.decide_help() == ("answer", "So, one more thing. How did you test the migration path?")


def test_decide_help_gives_openers_before_anyone_speaks():
    s = _convo()
    assert s.decide_help() == ("points", "")


def test_decide_help_without_me_marked_still_spots_a_question():
    s = Session()
    s.start()                                    # me_label is None
    s.add_utterance(1.0, "A", "Why did you pick Rust?")
    assert s.decide_help()[0] == "answer"


# -- names instead of numbers ---------------------------------------------------
def _heard(s, label, text, t=0.0):
    utt = s.add_utterance(t, label, text)
    return s.observe_name(utt)


def test_an_intro_names_the_voice_and_relabels_its_lines():
    s = _convo()                                  # A is me
    s.add_utterance(0.0, "B", "Good to see everyone.")
    assert _heard(s, "B", "Hi, I'm Sarah, I lead the platform team.") == [("B", "intro")]
    assert s.speaker_name("B") == "Sarah"
    assert s.name_sources["B"] == "intro"
    assert "Sarah: Good to see everyone." in s.transcript_text()   # earlier line too


def test_the_first_intro_wins():
    s = _convo()
    _heard(s, "B", "I'm Sarah.")
    assert _heard(s, "B", "My name is Priya.") == []
    assert s.speaker_name("B") == "Sarah"


def test_me_and_unknown_voices_are_never_named():
    s = _convo()
    assert _heard(s, "A", "I'm Daniel.") == []        # A is me
    assert _heard(s, "?", "I'm Sarah.") == []
    assert s.names == {}
    assert s.speaker_name("A") == "Me" and s.speaker_name("?") == "Speaker ?"


def test_a_typed_name_wins_and_clearing_returns_to_automatic():
    s = _convo()
    assert s.rename("B", "  Sarah   Lee ")
    assert s.speaker_name("B") == "Sarah Lee" and s.name_sources["B"] == "user"
    assert _heard(s, "B", "I'm Priya.") == []         # detection never overrides a typed name
    assert s.speaker_name("B") == "Sarah Lee"
    assert not s.rename("B", "Sarah Lee")              # already typed: unchanged
    assert s.rename("B", "")                           # empty: back to automatic
    assert s.speaker_name("B") == "Speaker B" and "B" not in s.name_sources
    assert not s.rename("B", "")                       # already automatic
    assert _heard(s, "B", "Sorry, my name is Priya.") == [("B", "intro")]
    assert s.speaker_name("B") == "Priya"


def test_my_own_name_marks_that_voice_as_me():
    s = Session(my_name="Daniel Tyukov")
    s.start()
    assert _heard(s, "B", "Hi, I'm Sarah.") == [("B", "intro")]
    assert _heard(s, "A", "Thanks, I'm Daniel.") == [("A", "me")]
    assert s.me_label == "A"
    assert "A" not in s.names and s.speaker_name("A") == "Me"


def test_my_name_from_a_second_voice_is_not_used_as_a_name():
    s = Session(my_name="Daniel")
    s.start()
    s.set_me("A")
    assert _heard(s, "C", "I'm Daniel.") == []        # my voice split into a new cluster
    assert s.me_label == "A" and "C" not in s.names


def test_heard_labels_in_first_heard_order():
    s = _convo()
    for label in ("B", "?", "A", "C", "B"):
        s.add_utterance(0.0, label, "x")
    assert s.heard_labels() == ["B", "A", "C"]


def test_a_new_meeting_starts_with_nobody_named():
    s = _convo()
    _heard(s, "B", "I'm Sarah.")
    s.start()
    assert s.names == {} and s.name_sources == {}


def test_export_uses_names_and_lists_participants(tmp_path):
    s = Session(my_name="Daniel")
    s.start(now=datetime(2026, 10, 3, 10, 0, 0))
    s.set_me("A")
    _heard(s, "B", "Hi, I'm Sarah.", t=1.0)
    s.add_utterance(2.0, "A", "Nice to meet you.")
    s.add_utterance(3.0, "C", "Can we start?")
    s.end(now=datetime(2026, 10, 3, 10, 5, 0))
    md = s.export_markdown(tmp_path)
    assert "- **Participants:** Me (Daniel), Sarah, Speaker C" in md
    assert "**[00:01] Sarah:** Hi, I'm Sarah." in md
    assert "**[00:02] Me:** Nice to meet you." in md
    assert "—" not in md.splitlines()[0]


def test_participants_without_my_name():
    s = _convo()
    s.add_utterance(0.0, "B", "Hello.")
    assert s.participants() == ["Me", "Speaker B"]


# -- names from the meeting (the roster) ----------------------------------------------
def _meeting(roster, me="A", my_name="Daniel"):
    s = Session(my_name=my_name)
    s.start()
    s.set_roster(roster)
    s.set_me(me)
    return s


def test_set_roster_leaves_me_out():
    s = Session(my_name="Daniel")
    s.set_roster(["Daniel Tyukov (Host)", "Sarah Chen", "sarah chen", "Room 4.12@corp"])
    assert s.roster == ["Sarah Chen"]


def test_an_intro_is_matched_to_the_roster():
    s = _meeting(["Sarah Chen", "Marcus Lee"])
    assert _heard(s, "B", "Hi, I'm Sarah.") == [("B", "intro")]
    assert s.speaker_name("B") == "Sarah Chen"


def test_a_one_to_one_names_the_other_voice_when_it_speaks():
    s = _meeting(["Sarah Chen"])
    _heard(s, "A", "Shall we start?")             # me
    assert _heard(s, "B", "Yes, let's.") == [("B", "roster")]
    assert s.speaker_name("B") == "Sarah Chen" and s.name_sources["B"] == "roster"


def test_elimination_waits_until_me_is_marked():
    s = _meeting(["Sarah Chen"], me=None)
    _heard(s, "A", "Shall we start?")
    assert _heard(s, "B", "Yes, let's.") == []   # A or B could be me: no guessing
    assert s.names == {}
    s.set_me("A")
    assert s.eliminate() == [("B", "roster")]
    assert s.speaker_name("B") == "Sarah Chen"


def test_the_last_voice_gets_the_last_name():
    s = _meeting(["Sarah Chen", "Marcus Lee"])
    _heard(s, "B", "Hi, I'm Sarah.")
    assert _heard(s, "C", "Morning all.") == [("C", "roster")]
    assert s.speaker_name("C") == "Marcus Lee"


def test_an_intro_replaces_a_roster_name_and_frees_the_other_voice():
    s = _meeting(["Sarah Chen", "Marcus Lee"])
    _heard(s, "B", "Hi, I'm Marcus.")
    _heard(s, "C", "Morning.")                   # C gets Sarah Chen by elimination
    assert s.names["C"] == "Sarah Chen"
    # diarization swapped them: C now says who it is, and it is Marcus
    _heard(s, "C", "Sorry, I'm Marcus Lee, I lead design.")
    assert s.names["C"] == "Marcus Lee" and s.name_sources["C"] == "intro"
    assert s.names["B"] == "Marcus Lee"          # an intro is never undone by another intro

    t = _meeting(["Sarah Chen", "Marcus Lee"])
    _heard(t, "B", "Hi, I'm Marcus.")
    _heard(t, "C", "Morning.")                   # C: Sarah Chen, by elimination
    _heard(t, "D", "Hi, I'm Sarah.")             # the real Sarah introduces herself
    assert t.names["D"] == "Sarah Chen" and t.name_sources["D"] == "intro"
    assert "C" not in t.names                    # the guess is withdrawn
    assert t.speaker_name("C") == "Speaker C"


def test_typed_names_beat_intros_and_the_roster():
    s = _meeting(["Sarah Chen", "Marcus Lee"])
    _heard(s, "B", "Hi, I'm Marcus.")
    _heard(s, "C", "Morning.")                   # C: Sarah Chen by elimination
    assert s.rename("B", "Sarah Chen")           # I know better
    assert s.name_sources["B"] == "user" and "C" not in s.names
    # a typed name stays; the freed voice then gets the one name left over
    assert _heard(s, "B", "I'm Marcus.") == [("C", "roster")]
    assert s.names == {"B": "Sarah Chen", "C": "Marcus Lee"}


def test_participants_are_the_union_of_voices_and_roster():
    s = _meeting(["Sarah Chen", "Marcus Lee", "Priya Raman"])
    _heard(s, "A", "Hello.")
    _heard(s, "B", "Hi, I'm Sarah.")
    _heard(s, "C", "Hello there.")
    assert s.participants() == ["Me (Daniel)", "Sarah Chen", "Speaker C", "Marcus Lee",
                                "Priya Raman"]
    md = s.export_markdown("/tmp")
    assert "- **Participants:** Me (Daniel), Sarah Chen, Speaker C, Marcus Lee, Priya Raman" in md


def test_a_new_meeting_keeps_the_roster():
    s = _meeting(["Sarah Chen"])
    s.start()
    assert s.roster == ["Sarah Chen"]


def test_participants_list_a_person_once_when_two_voices_share_the_name():
    s = _meeting(["Sarah Chen", "Marcus Lee"])
    _heard(s, "B", "Hi, I'm Sarah.")
    s.add_utterance(1.0, "C", "More from Sarah, split off by diarization.")
    s.rename("C", "sarah chen")
    assert s.participants() == ["Me (Daniel)", "Sarah Chen", "Marcus Lee"]


def test_clearing_an_eliminated_name_keeps_elimination_off_that_voice():
    s = _meeting(["Sarah Chen"])
    _heard(s, "B", "Morning.")
    assert s.names == {"B": "Sarah Chen"}
    assert s.rename("B", "")                     # a wrong guess, dismissed
    assert _heard(s, "B", "Shall we?") == []     # it does not snap back
    assert s.speaker_name("B") == "Speaker B"
    assert _heard(s, "B", "Sorry, I'm Priya.") == [("B", "intro")]   # an intro still names it
    assert s.speaker_name("B") == "Priya"


def test_clearing_a_typed_name_leaves_elimination_free_to_act():
    s = _meeting(["Sarah Chen"])
    s.add_utterance(0.0, "B", "Morning.")
    s.rename("B", "Bob")
    s.rename("B", "")                            # typed, then cleared: fully automatic
    assert _heard(s, "B", "Shall we?") == [("B", "roster")]
    assert s.speaker_name("B") == "Sarah Chen"


def test_naming_a_declined_voice_lifts_the_decline():
    s = _meeting(["Sarah Chen"])
    _heard(s, "B", "Morning.")
    s.rename("B", "")                            # decline the guess
    s.rename("B", "Bob")                         # then name it by hand
    s.rename("B", "")                            # and clear that: fully automatic again
    assert _heard(s, "B", "Shall we?") == [("B", "roster")]

    t = _meeting(["Sarah Chen", "Marcus Lee"])
    _heard(t, "B", "Hi, I'm Sarah.")
    _heard(t, "C", "Hello.")                     # the last voice: Marcus Lee
    assert t.names["C"] == "Marcus Lee"
    t.rename("C", "")                            # decline C's guess
    assert "C" in t.declined
    _heard(t, "C", "I'm Tom.")                   # an intro names it, lifting the decline
    assert "C" not in t.declined


def test_typing_the_name_a_voice_already_has_makes_it_a_typed_name():
    s = _meeting(["Sarah Chen"])
    _heard(s, "B", "Morning.")                   # Sarah Chen, by elimination
    assert s.name_sources["B"] == "roster"
    assert s.rename("B", "Sarah Chen")           # confirmed by hand
    assert s.name_sources["B"] == "user"
    assert not s.rename("B", "Sarah Chen")       # now truly unchanged

    t = _convo()
    _heard(t, "B", "Hi, I'm Sarah.")
    assert t.rename("B", "Sarah") and t.name_sources["B"] == "user"


def test_a_new_meeting_forgets_who_was_me():
    s = _convo()                                 # A is me
    s.start()                                    # letters restart with the new stream
    assert s.me_label is None
