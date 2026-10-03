from pathlib import Path

import numpy as np

from meeting_copilot.audio import FRAME_SAMPLES
from meeting_copilot.engine import CopilotEngine, EngineConfig


class FakeSource:
    def __init__(self, frames):
        self._frames = frames

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def frames(self):
        yield from self._frames


class FakeTranscriber:
    def __init__(self, text):
        self.text = text

    def transcribe(self, audio):
        return self.text


def _tone(i, freq=200.0, amp=0.2):
    t = (np.arange(FRAME_SAMPLES) + i * FRAME_SAMPLES) / 16000
    return (np.sin(2 * np.pi * freq * t) * amp * 32768).astype(np.int16)


def _silence():
    return np.zeros(FRAME_SAMPLES, dtype=np.int16)


def _scripted_frames():
    frames = [_silence() for _ in range(20)]
    frames += [_tone(i) for i in range(40)]      # one utterance
    frames += [_silence() for _ in range(30)]    # flush it
    return frames


def _engine(tmp_path, **kw):
    kw.setdefault("stt_backend", "local")
    kw.setdefault("diarize", False)
    cfg = EngineConfig(root=tmp_path, **kw)
    events = []
    eng = CopilotEngine(cfg, on_event=events.append)
    eng._transcriber = FakeTranscriber("hello world")  # avoid loading real Whisper
    return eng, events


def test_pipeline_emits_utterance_and_exports(tmp_path):
    eng, events = _engine(tmp_path)
    eng.start_meeting()
    eng.run(FakeSource(_scripted_frames()))
    path = eng.end_meeting()  # finish() drains the worker

    utts = [e for e in events if e["type"] == "utterance"]
    assert len(utts) == 1
    assert utts[0]["text"] == "hello world"
    assert len(eng.session.utterances) == 1
    assert path is not None and path.exists()
    assert "hello world" in path.read_text()


def test_ignores_audio_when_not_recording(tmp_path):
    eng, events = _engine(tmp_path)
    # never call start_meeting -> no backend, feed is skipped
    eng.run(FakeSource(_scripted_frames()))
    assert [e for e in events if e["type"] == "utterance"] == []


def test_request_help_uses_assistant(tmp_path, monkeypatch):
    eng, events = _engine(tmp_path)
    eng.context = "PROJECT CONTEXT"
    eng.start_meeting()
    eng.session.add_utterance(1.0, "B", "What did you build?")

    monkeypatch.setattr(eng.assistant, "answer",
                        lambda ctx, tr, q, note="", on_delta=None, mode="answer": f"I built it. ({q}) [{note}]")
    eng.request_help(note="focus on the data layer")
    eng.end_meeting()

    helps = [e for e in events if e["type"] == "help"]
    assert len(helps) == 1
    assert "What did you build?" in helps[0]["answer"]
    assert "focus on the data layer" in helps[0]["answer"]  # note threaded through
    assert len(eng.session.assists) == 1


def test_cycle_answer_model(tmp_path):
    eng, events = _engine(tmp_path)
    eng.set_answer_model("haiku")
    assert eng.cfg.answer_model == "haiku"
    assert eng.assistant.model == "haiku"
    eng.cycle_answer_model()  # haiku -> sonnet
    assert eng.cfg.answer_model == "sonnet"
    assert any(e["type"] == "model" for e in events)


def test_api_primary_when_key_present(tmp_path):
    from meeting_copilot.assistant import ChainAssistant
    cfg = EngineConfig(root=tmp_path, anthropic_api_key="sk-test")
    eng = CopilotEngine(cfg)
    assert eng.answer_primary == "api"
    assert isinstance(eng.assistant, ChainAssistant)
    # chain always ends with the offline local backend
    assert [n for n, _, _ in eng.assistant.backends] == ["api", "cli", "local"]


def test_cli_primary_without_key(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    eng = CopilotEngine(EngineConfig(root=tmp_path))
    assert eng.answer_primary == "cli"
    assert [n for n, _, _ in eng.assistant.backends] == ["cli", "local"]


def test_offline_routes_stt_and_answers_local(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    from meeting_copilot.backends import LocalBackend
    cfg = EngineConfig(root=tmp_path, stt_backend="deepgram", deepgram_api_key="x")
    events = []
    eng = CopilotEngine(cfg, on_event=events.append)
    eng._transcriber = FakeTranscriber("x")           # avoid loading real Whisper
    monkeypatch.setattr(eng.monitor, "_online", False)  # simulate OFFLINE
    # STT: offline -> local backend, not Deepgram
    assert isinstance(eng._make_backend(), LocalBackend)
    # Answers: offline -> chain skips network, would use local (no api/cli attempt)
    assert eng.online is False


def test_export_falls_back_when_launch_dir_missing(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    missing = tmp_path / "deleted-launch-dir"   # never created
    eng, events = _engine(missing)
    eng.session.start()
    eng.session.add_utterance(1.0, "?", "hello world")
    path = eng._safe_export()
    assert path is not None and path.exists()
    assert path.parent == home                  # fell back to home
    assert "hello world" in path.read_text()
    assert any(e["type"] == "exported" for e in events)


def test_request_help_with_nothing_said_drafts_openers(tmp_path, monkeypatch):
    eng, events = _engine(tmp_path)
    eng.start_meeting()
    monkeypatch.setattr(eng.assistant, "answer",
                        lambda c, t, q, note="", on_delta=None, mode="answer": f"{mode}|{q!r}")
    eng.request_help()                       # auto: nothing to answer, so openers
    eng.end_meeting()
    helps = [e for e in events if e["type"] == "help"]
    assert helps and helps[0]["mode"] == "points" and helps[0]["answer"] == "points|''"


def test_forced_answer_without_question_says_so(tmp_path):
    eng, events = _engine(tmp_path)
    eng.start_meeting()
    eng.request_help(mode="answer")          # nothing to answer: no backend is called
    eng.end_meeting()
    assert any(e["type"] == "info" for e in events)
    assert not any(e["type"] == "help" for e in events)


def test_cycle_me_walks_the_voices_actually_heard(tmp_path):
    eng, events = _engine(tmp_path)
    eng.cycle_me()                               # nobody heard yet: nothing to mark
    assert eng.session.me_label is None
    eng.start_meeting()
    for label in ("B", "A", "C", "B"):
        eng._on_final(f"line from {label}", label)
    eng._on_final("unattributed", None)          # "?" is never a candidate
    seen = []
    for _ in range(4):
        eng.cycle_me()
        seen.append(eng.session.me_label)
    eng.end_meeting()
    assert seen == ["B", "A", "C", None]         # first heard first, then back to none
    mes = [e for e in events if e["type"] == "me"]
    assert [e["label"] for e in mes][-4:] == seen
    assert any(e["type"] == "names" and e["me"] == "C" for e in events)


def test_engine_builds_stt_fallback(tmp_path):
    from meeting_copilot.backends import FallbackSttBackend
    cfg = EngineConfig(root=tmp_path, stt_backend="deepgram",
                       deepgram_api_key="x", stt_fallback=True)
    eng = CopilotEngine(cfg)
    assert eng.stt_mode == "deepgram→local"
    assert isinstance(eng._make_backend(), FallbackSttBackend)


def test_engine_stt_fallback_disabled(tmp_path):
    from meeting_copilot.backends import DeepgramBackend
    cfg = EngineConfig(root=tmp_path, stt_backend="deepgram",
                       deepgram_api_key="x", stt_fallback=False)
    eng = CopilotEngine(cfg)
    assert eng.stt_mode == "deepgram"
    assert isinstance(eng._make_backend(), DeepgramBackend)


def test_help_reports_served_backend(tmp_path, monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    eng, events = _engine(tmp_path)   # local STT + CLI answers
    eng.start_meeting()
    eng.session.add_utterance(1.0, "B", "Q?")
    monkeypatch.setattr(eng.assistant, "answer",
                        lambda c, t, q, note="", on_delta=None, mode="answer": "An answer.")
    eng.request_help()
    eng.end_meeting()
    helps = [e for e in events if e["type"] == "help"]
    assert helps and helps[0]["served"] == "cli"


# -- talking points -----------------------------------------------------------
def test_request_points_works_without_any_question(tmp_path, monkeypatch):
    eng, events = _engine(tmp_path)
    eng.context = "PROJECT CONTEXT"
    eng.start_meeting()                              # nobody has spoken yet
    seen = {}

    def fake(ctx, tr, q, note="", on_delta=None, mode="answer"):
        seen.update(q=q, mode=mode)
        return "- I could open with the v2 launch."

    monkeypatch.setattr(eng.assistant, "answer", fake)
    eng.request_help(mode="points")
    eng.end_meeting()
    assert seen["mode"] == "points" and seen["q"] == ""
    helps = [e for e in events if e["type"] == "help"]
    assert helps and helps[0]["mode"] == "points"
    started = [e for e in events if e["type"] == "help_started"]
    assert started and started[0]["mode"] == "points"
    assert eng.session.assists[0].kind == "points"


def test_request_points_anchors_on_the_last_line(tmp_path, monkeypatch):
    eng, events = _engine(tmp_path)
    eng.start_meeting()
    eng.session.add_utterance(1.0, "B", "Why Rust?")
    eng.session.add_utterance(2.0, "A", "Because of the borrow checker.")
    seen = {}
    monkeypatch.setattr(eng.assistant, "answer",
                        lambda c, t, q, note="", on_delta=None, mode="answer": seen.update(q=q) or "x")
    eng.request_help(mode="points")
    eng.end_meeting()
    assert seen["q"] == "Because of the borrow checker."


def test_request_help_decides_the_mode_from_the_transcript(tmp_path, monkeypatch):
    eng, events = _engine(tmp_path)
    eng.start_meeting()
    eng.session.set_me("A")
    monkeypatch.setattr(eng.assistant, "answer",
                        lambda c, t, q, note="", on_delta=None, mode="answer": f"{mode}|{q}")
    eng.session.add_utterance(1.0, "B", "Why Rust?")
    eng.request_help()                                   # a question is pending: answer it
    eng.session.add_utterance(2.0, "A", "Because of the borrow checker.")
    eng.request_help()                                   # I just spoke: keep going
    eng.end_meeting()
    helps = [e for e in events if e["type"] == "help"]
    assert [h["mode"] for h in helps] == ["answer", "points"]
    assert helps[0]["answer"] == "answer|Why Rust?"
    assert helps[1]["answer"] == "points|Because of the borrow checker."
    assert [a.kind for a in eng.session.assists] == ["answer", "points"]


def test_request_help_explicit_mode_overrides_the_decision(tmp_path, monkeypatch):
    eng, events = _engine(tmp_path)
    eng.start_meeting()
    eng.session.set_me("A")
    eng.session.add_utterance(1.0, "B", "Why Rust?")
    monkeypatch.setattr(eng.assistant, "answer",
                        lambda c, t, q, note="", on_delta=None, mode="answer": mode)
    eng.request_help(mode="points")
    eng.end_meeting()
    helps = [e for e in events if e["type"] == "help"]
    assert helps[0]["mode"] == "points"


# -- names ---------------------------------------------------------------------
def test_an_intro_emits_names_before_the_utterance(tmp_path):
    eng, events = _engine(tmp_path)
    eng.start_meeting()
    eng._on_final("Hi, I'm Sarah, I lead the platform team.", "B")
    eng._on_final("I'm going to share my screen.", "B")
    eng.end_meeting()
    kinds = [e["type"] for e in events if e["type"] in ("names", "utterance")]
    # start resets names; then the intro names B before its line is shown
    assert kinds == ["names", "names", "utterance", "utterance"]
    named = [e for e in events if e["type"] == "names"][1]
    assert named["names"] == {"B": "Sarah"} and named["label"] == "B"
    assert named["name"] == "Sarah" and named["source"] == "intro"
    utts = [e for e in events if e["type"] == "utterance"]
    assert [u["name"] for u in utts] == ["Sarah", "Sarah"]
    assert "Sarah:** I'm going to share my screen." in eng.session.export_markdown(tmp_path)


def test_partials_never_name_a_voice(tmp_path):
    eng, events = _engine(tmp_path)
    eng.start_meeting()
    eng._on_partial("Hi, I'm Sarah", "B")
    eng.end_meeting()
    assert eng.session.names == {}
    assert [e for e in events if e["type"] == "names" and e.get("label")] == []


def test_my_name_marks_me_and_reaches_the_prompt(tmp_path, monkeypatch):
    eng, events = _engine(tmp_path, my_name="Daniel")
    assert all(a.my_name == "Daniel" for _, a, _ in eng.assistant.backends)
    eng.start_meeting()
    eng._on_final("Hi, thanks for having me, I'm Daniel.", "A")
    me = [e for e in events if e["type"] == "me"]
    assert me and me[-1]["label"] == "A" and me[-1]["by"] == "name"
    assert eng.session.me_label == "A" and eng.session.names == {}
    assert [e["name"] for e in events if e["type"] == "utterance"] == ["Me"]

    from meeting_copilot.assistant import build_user_prompt
    seen = {}

    def fake(c, t, q, note="", on_delta=None, mode="answer"):
        cli = eng.assistant.backends[0][1]
        seen["prompt"] = build_user_prompt(c, t, q, note, mode, cli.my_name)
        return "x"

    monkeypatch.setattr(eng.assistant, "answer", fake)
    eng.request_help(mode="points")
    eng.end_meeting()
    assert "My name is Daniel." in seen["prompt"]
    assert "Me: Hi, thanks for having me" in seen["prompt"]


def test_rename_speaker_emits_names(tmp_path):
    eng, events = _engine(tmp_path)
    eng.start_meeting()
    eng._on_final("Morning.", "B")
    eng.rename_speaker("B", "Sarah")
    eng.rename_speaker("B", "Sarah")             # no change, no event
    eng.rename_speaker("B", "")                  # back to automatic
    eng.end_meeting()
    named = [e for e in events if e["type"] == "names" and e.get("label") == "B"]
    assert [(e["name"], e["source"]) for e in named] == [("Sarah", "user"), ("Speaker B", None)]
    assert eng.session.names == {}


# -- a newer draft supersedes one still streaming --------------------------------
def test_a_newer_help_supersedes_a_streaming_draft(tmp_path, monkeypatch):
    import threading
    eng, events = _engine(tmp_path)
    eng.start_meeting()
    eng.session.add_utterance(1.0, "B", "Why Rust?")
    first_streaming, release_first = threading.Event(), threading.Event()

    def fake(c, t, q, note="", on_delta=None, mode="answer"):
        if note == "first":
            on_delta("old 1")
            first_streaming.set()
            release_first.wait(5)
            on_delta("old 1 2")                  # arrives after the second press
            return "old draft"
        on_delta("new")
        return "new draft"

    monkeypatch.setattr(eng.assistant, "answer", fake)
    t1 = threading.Thread(target=eng.request_help, kwargs={"note": "first"})
    t1.start()
    assert first_streaming.wait(5)
    eng.request_help(note="second")              # the second press, while the first streams
    release_first.set()
    t1.join(5)
    eng.end_meeting()

    deltas = [e["text"] for e in events if e["type"] == "help_delta"]
    assert deltas == ["old 1", "new"]            # nothing from the first after the second began
    helps = [e["answer"] for e in events if e["type"] == "help"]
    assert helps == ["new draft"]
    assert [a.answer for a in eng.session.assists] == ["new draft"]


def test_a_superseded_draft_does_not_report_its_backend_failing(tmp_path, monkeypatch):
    import threading
    from meeting_copilot.assistant import AssistantError
    eng, events = _engine(tmp_path)
    eng.start_meeting()
    started, release = threading.Event(), threading.Event()
    cli = eng.assistant.backends[0][1]
    monkeypatch.setattr(cli, "is_available", lambda: True)
    local = eng.assistant.backends[-1][1]
    monkeypatch.setattr(local, "is_available", lambda: False)

    def fake(c, t, q, note="", on_delta=None, mode="answer"):
        if note == "first":
            started.set()
            release.wait(5)
            raise AssistantError("boom")
        return "new draft"

    monkeypatch.setattr(cli, "answer", fake)
    t1 = threading.Thread(target=eng.request_help, kwargs={"note": "first"})
    t1.start()
    assert started.wait(5)
    eng.request_help(note="second")
    release.set()
    t1.join(5)
    eng.end_meeting()
    assert not any(e["type"] == "answer_switch" for e in events)
    assert not any(e["type"] == "error" for e in events)
    assert [e["answer"] for e in events if e["type"] == "help"] == ["new draft"]


def test_a_failed_draft_says_so(tmp_path, monkeypatch):
    from meeting_copilot.assistant import AssistantError
    eng, events = _engine(tmp_path)
    eng.start_meeting()

    def boom(*a, **k):
        raise AssistantError("no backend")

    monkeypatch.setattr(eng.assistant, "answer", boom)
    eng.request_help()
    eng.end_meeting()
    errors = [e for e in events if e["type"] == "error"]
    assert errors and errors[0]["help"] is True


# -- the mic dot ------------------------------------------------------------------
def test_capture_loop_reports_the_level_on_and_off(tmp_path):
    eng, events = _engine(tmp_path)
    eng.run(FakeSource(_scripted_frames()))      # not recording: the dot still works
    levels = [e["on"] for e in events if e["type"] == "level"]
    assert levels == [True, False]
    assert events[-1]["type"] == "audio_stopped"


def test_level_turns_off_when_the_source_ends_mid_speech(tmp_path):
    eng, events = _engine(tmp_path)
    eng.run(FakeSource([_tone(i) for i in range(20)]))
    assert [e["on"] for e in events if e["type"] == "level"] == [True, False]


# -- the roster: --people, --invite, or the .ics in the launch directory ---------------
_INVITE = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n"
           "ORGANIZER;CN=Daniel Tyukov:mailto:daniel@example.com\r\n"
           "ATTENDEE;CN=Sarah Chen:mailto:sarah@example.com\r\n"
           "ATTENDEE;CN=\"Lee, Marcus\":mailto:marcus@example.com\r\n"
           "END:VEVENT\r\nEND:VCALENDAR\r\n")


def _infos(events):
    return [e["msg"] for e in events if e["type"] == "info"]


def test_people_flag_builds_the_roster(tmp_path):
    eng, events = _engine(tmp_path, my_name="Daniel", people="Daniel Tyukov, Sarah Chen")
    (tmp_path / "ignored.ics").write_text(_INVITE)   # --people means no invite lookup
    eng.load_roster()
    assert eng.session.roster == ["Sarah Chen"]
    assert all(a.people == ["Sarah Chen"] for _, a, _ in eng.assistant.backends)
    assert "People in this meeting: Sarah Chen." in _infos(events)
    names = [e for e in events if e["type"] == "names"]
    assert names[-1]["roster"] == ["Sarah Chen"]
    assert eng.roster_source == "--people"


def test_invite_flag(tmp_path):
    invite = tmp_path / "elsewhere" / "sync.ics"
    invite.parent.mkdir()
    invite.write_text(_INVITE)
    eng, events = _engine(tmp_path, my_name="Daniel Tyukov", invite=invite)
    eng.load_roster()
    assert eng.session.roster == ["Sarah Chen", "Marcus Lee"]
    assert eng.roster_source == "sync.ics"


def test_people_and_invite_together(tmp_path):
    invite = tmp_path / "sync.ics"
    invite.write_text(_INVITE)
    eng, _ = _engine(tmp_path, my_name="Daniel", people="Priya Raman", invite=invite)
    eng.load_roster()
    assert eng.session.roster == ["Priya Raman", "Sarah Chen", "Marcus Lee"]
    assert eng.roster_source == "--people and sync.ics"


def test_the_invite_in_the_launch_directory_is_found_and_announced(tmp_path):
    (tmp_path / "Team sync.ics").write_text(_INVITE)
    eng, events = _engine(tmp_path, my_name="Daniel")
    eng.load_roster()
    assert eng.session.roster == ["Sarah Chen", "Marcus Lee"]
    assert ("Using the invite Team sync.ics from this directory: Sarah Chen, Marcus Lee."
            in _infos(events))


def test_several_invites_are_not_guessed_between(tmp_path):
    (tmp_path / "a.ics").write_text(_INVITE)
    (tmp_path / "b.ics").write_text(_INVITE)
    eng, events = _engine(tmp_path)
    eng.load_roster()
    assert eng.session.roster == []
    assert any("Several .ics files" in m for m in _infos(events))


def test_no_invite_and_no_flags_is_quiet(tmp_path):
    eng, events = _engine(tmp_path)
    eng.load_roster()
    assert eng.session.roster == [] and _infos(events) == []
    assert eng.roster_source is None


def test_an_unreadable_invite_is_an_error_not_a_crash(tmp_path):
    eng, events = _engine(tmp_path, invite=tmp_path / "missing.ics")
    eng.load_roster()
    assert any(e["type"] == "error" and "missing.ics" in e["msg"] for e in events)
    assert eng.session.roster == []


def test_prepare_loads_the_roster(tmp_path, monkeypatch):
    (tmp_path / "sync.ics").write_text(_INVITE)
    eng, events = _engine(tmp_path, my_name="Daniel")
    monkeypatch.setattr(eng.monitor, "start", lambda: None)
    eng._transcriber = FakeTranscriber("x")
    eng.prepare()
    assert eng.session.roster == ["Sarah Chen", "Marcus Lee"]
    assert events[-1]["type"] == "ready"


def test_marking_me_lets_elimination_name_the_other_voice(tmp_path):
    eng, events = _engine(tmp_path, people="Sarah Chen")
    eng.load_roster()
    eng.start_meeting()
    eng._on_final("Shall we start?", "A")
    eng._on_final("Yes, let's.", "B")
    assert eng.session.names == {}                    # nobody is marked as me yet
    eng.cycle_me()                                     # A is me
    eng.end_meeting()
    assert eng.session.names == {"B": "Sarah Chen"}
    roster_named = [e for e in events if e["type"] == "names" and e.get("source") == "roster"]
    assert roster_named and roster_named[-1]["label"] == "B"
    assert roster_named[-1]["name"] == "Sarah Chen"


def test_intro_by_my_name_then_elimination_in_one_final(tmp_path):
    eng, events = _engine(tmp_path, my_name="Daniel", people="Sarah Chen")
    eng.load_roster()
    eng.start_meeting()
    eng._on_final("Good to see you.", "B")
    eng._on_final("Hi, I'm Daniel.", "A")              # marks me, which settles B
    eng.end_meeting()
    assert eng.session.me_label == "A" and eng.session.names == {"B": "Sarah Chen"}
    kinds = [(e["type"], e.get("source") or e.get("by")) for e in events
             if e["type"] in ("me", "names") and e.get("label")]
    assert kinds[-3:] == [("me", "name"), ("names", "me"), ("names", "roster")]


def test_a_second_meeting_starts_with_nobody_marked_as_me(tmp_path):
    eng, events = _engine(tmp_path)
    eng.start_meeting()
    eng._on_final("Hello.", "A")
    eng.cycle_me()
    assert eng.session.me_label == "A"
    eng.end_meeting()
    eng.start_meeting()                               # new stream, letters restart
    eng.end_meeting()
    assert eng.session.me_label is None
    last_names = [e for e in events if e["type"] == "names"][-1]
    assert last_names["me"] is None and last_names["names"] == {}


def test_only_cleaned_roster_names_reach_the_prompt(tmp_path, monkeypatch):
    long_name = "A display name that goes on and on well past sixty characters in all"
    eng, _ = _engine(tmp_path, my_name="Daniel", people=(
        f"Sarah Chen (Host); Add people; mic_off; {long_name}; sarah@example.com; "
        "Participants (4); Daniel Tyukov (You); Marcus Lee"))
    eng.load_roster()
    assert eng.session.roster == ["Sarah Chen", "Marcus Lee"]
    eng.start_meeting()
    seen = {}

    def fake(c, t, q, note="", on_delta=None, mode="answer"):
        cli = eng.assistant.backends[0][1]
        seen["prompt"] = cli.build_user_prompt(c, t, q, note, mode)
        return "x"

    monkeypatch.setattr(eng.assistant, "answer", fake)
    eng.request_help()
    eng.end_meeting()
    assert "PEOPLE IN THIS MEETING: Sarah Chen, Marcus Lee\n" in seen["prompt"]
    for junk in ("Add people", "mic_off", long_name, "@", "Participants", "(Host)", "Tyukov"):
        assert junk not in seen["prompt"]
