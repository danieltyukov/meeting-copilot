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
