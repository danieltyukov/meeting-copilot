import threading

from rich.console import Console

from meeting_copilot.app import CopilotTUI
from meeting_copilot.engine import CopilotEngine, EngineConfig


def _console(**kwargs):
    # A CI runner has no VT console, so on Windows Rich would fall back to its
    # legacy renderer (square box corners). Real terminals there support VT.
    return Console(legacy_windows=False, **kwargs)


def _tui(tmp_path):
    eng = CopilotEngine(EngineConfig(root=tmp_path))
    return CopilotTUI(eng, source_factory=lambda: None)


def test_on_event_reentrant_under_render_lock(tmp_path):
    """The render loop holds _lock; an event arriving must not deadlock."""
    tui = _tui(tmp_path)
    done = threading.Event()

    def run():
        with tui._lock:                       # render loop / keyboard holds the lock
            tui._on_event({"type": "me", "label": "A"})  # engine emit re-enters it
        done.set()

    threading.Thread(target=run, daemon=True).start()
    assert done.wait(timeout=5), "deadlock: _on_event re-entered _lock"


def test_command_keys_do_not_deadlock(tmp_path):
    """Pressing keys whose handlers emit events must not freeze the UI."""
    tui = _tui(tmp_path)
    done = threading.Event()

    def press():
        tui._command_key("m")   # cycle_me -> emits "me"
        tui._command_key("1")   # set_answer_model -> emits "model"
        done.set()

    threading.Thread(target=press, daemon=True).start()
    assert done.wait(timeout=5), "deadlock: command key dispatch"


def test_compose_then_help_flow(tmp_path):
    """Compose a note, save it, and confirm it's queued for the next help."""
    tui = _tui(tmp_path)
    tui._command_key("c")
    assert tui.compose
    for ch in "focus on scaling":
        tui._compose_key(ch)
    tui._compose_key("\r")               # Enter saves
    assert not tui.compose
    assert tui.note == "focus on scaling"


def _long_answer(n):
    return "\n\n".join(f"Paragraph number {i} with several words to wrap." for i in range(n))


def test_long_answer_grows_to_fit_tall_window(tmp_path):
    tui = _tui(tmp_path)
    tui.console = _console(width=90, height=44, record=True)
    tui.answer_question = "Why this design?"
    tui.answer = _long_answer(8)
    tui.console.print(tui._render())
    out = tui.console.export_text()
    assert "Paragraph number 7" in out      # last paragraph visible, not capped
    assert "↑/↓ scroll" not in out           # no scrolling needed


def test_long_answer_scrolls_in_short_window(tmp_path):
    tui = _tui(tmp_path)
    tui.answer_question = "Why this design?"
    tui.answer = _long_answer(20)

    def render():
        tui.console = _console(width=90, height=15, record=True)
        tui.console.print(tui._render())
        return tui.console.export_text()

    top = render()
    assert "↑/↓ scroll" in top                            # capped -> scrollable
    assert "Paragraph number 0" in top
    assert "Paragraph number 19" not in top               # bottom hidden
    for _ in range(80):
        tui._handle_arrow("down")                         # scroll to the end
    bottom = render()
    assert "Paragraph number 19" in bottom                # now visible


def test_help_box_renders_above_transcript(tmp_path):
    """The answer/help box sits ABOVE the live transcript, not below it."""
    tui = _tui(tmp_path)

    def render():
        tui.console = _console(width=90, height=44, record=True)
        tui.console.print(tui._render())
        return tui.console.export_text()

    # Empty (initial) state: the placeholder help box still leads the transcript.
    out = render()
    assert out.index("Help: read this aloud") < out.index("Live transcript")

    # With a drafted answer the ordering must hold too.
    tui.answer_question = "Why this design?"
    tui.answer = "Because the thing you read aloud should be at eye level."
    out = render()
    assert out.index("Help: read this aloud") < out.index("Live transcript")


def _tui_api_deepgram(tmp_path):
    eng = CopilotEngine(EngineConfig(root=tmp_path, stt_backend="deepgram",
                                     deepgram_api_key="x", anthropic_api_key="x"))
    tui = CopilotTUI(eng, source_factory=lambda: None)
    tui.console = _console(width=130, height=30, record=True)
    return tui


def test_header_normal_when_no_switch(tmp_path):
    tui = _tui_api_deepgram(tmp_path)
    tui.console.print(tui._render())
    out = tui.console.export_text()
    assert "Deepgram nova-3" in out and "Sonnet via API" in out
    assert "fallback" not in out


def test_header_shows_fallback_tags_after_switch(tmp_path):
    tui = _tui_api_deepgram(tmp_path)
    tui._on_event({"type": "stt_switch", "backend": "local", "reason": "dropped"})
    tui._on_event({"type": "help", "question": "Q?", "answer": "A", "served": "cli"})
    tui.console.print(tui._render())
    out = tui.console.export_text()
    assert "Whisper base (fallback)" in out      # STT switched, shown in header
    assert "Sonnet via CLI (fallback)" in out    # answers served by CLI, shown in header
    assert tui.stt_active == "local" and tui.answer_active == "cli"


def test_header_online_offline_marker(tmp_path):
    tui = _tui_api_deepgram(tmp_path)
    tui.console.print(tui._render())
    assert "online" in tui.console.export_text()

    tui._on_event({"type": "connectivity", "online": False})
    assert tui.online is False
    tui.console = _console(width=130, height=30, record=True)
    tui.console.print(tui._render())
    assert "OFFLINE" in tui.console.export_text()


def test_header_local_answer_shows_ollama_model(tmp_path):
    tui = _tui_api_deepgram(tmp_path)
    tui._on_event({"type": "help", "question": "Q?", "answer": "A", "served": "local"})
    tui.console.print(tui._render())
    out = tui.console.export_text()
    # header shows the configured local LLM (the Ollama default) served by Ollama
    assert f"{tui.engine.cfg.ollama_model} via Ollama" in out


def test_help_served_updates_answer_active(tmp_path):
    tui = _tui_api_deepgram(tmp_path)
    tui._on_event({"type": "help", "question": "Q?", "answer": "A", "served": "cli"})
    assert tui.answer_active == "cli"
    tui._on_event({"type": "help", "question": "Q2?", "answer": "A2", "served": "api"})
    assert tui.answer_active == "api"   # reverts when the API works again


# -- one key, two kinds of draft ----------------------------------------------
def test_h_key_asks_the_engine_to_decide(tmp_path, monkeypatch):
    tui = _tui(tmp_path)
    calls = []
    monkeypatch.setattr(tui.engine, "request_help",
                        lambda note="", mode="auto": calls.append((note, mode)))
    tui.note = "keep it short"
    tui._command_key("h")
    for _ in range(50):                      # the handler runs on a thread
        if calls:
            break
        threading.Event().wait(0.02)
    assert calls == [("keep it short", "auto")]
    assert tui.note == ""                    # the queued note is consumed


def test_t_is_not_a_command(tmp_path, monkeypatch):
    tui = _tui(tmp_path)
    calls = []
    monkeypatch.setattr(tui.engine, "request_help", lambda *a, **k: calls.append(1))
    tui._command_key("t")
    threading.Event().wait(0.05)
    assert calls == []


def test_answer_panel_title_follows_the_mode(tmp_path):
    tui = _tui(tmp_path)

    def render():
        tui.console = _console(width=90, height=30, record=True)
        tui.console.print(tui._render())
        return tui.console.export_text()

    tui._on_event({"type": "help_started", "question": "Why?", "note": "", "mode": "points"})
    tui._on_event({"type": "help", "question": "Why?", "answer": "- a point", "served": "cli",
                   "mode": "points"})
    out = render()
    assert "Talking points" in out and "From: Why?" in out
    tui._on_event({"type": "help_started", "question": "Why?", "note": "", "mode": "answer"})
    tui._on_event({"type": "help", "question": "Why?", "answer": "Because.", "served": "cli",
                   "mode": "answer"})
    out = render()
    assert "read this aloud" in out and "Q: Why?" in out and "Talking points" not in out


def test_footer_lists_one_help_key(tmp_path):
    tui = _tui(tmp_path)
    tui.console = _console(width=120, height=30, record=True)
    tui.console.print(tui._render())
    out = tui.console.export_text()          # export clears the record: read it once
    assert " h  help " in out and " t  points" not in out


# -- names on screen ---------------------------------------------------------------
def _render_text(tui, width=100, height=30):
    tui.console = _console(width=width, height=height, record=True)
    tui.console.print(tui._render())
    return tui.console.export_text()


def _transcript_rows(out):
    """Just the transcript panel's rows, so a status line naming a voice does not count."""
    rows = out.splitlines()
    top = next(i for i, r in enumerate(rows) if "Live transcript" in r)
    bottom = next(i for i in range(top + 1, len(rows)) if rows[i].startswith("╰"))
    return "\n".join(rows[top + 1:bottom])


def _say(tui, t, label, text):
    tui._on_event({"type": "utterance", "t": t, "speaker": label, "name": "", "text": text})


def test_naming_a_voice_relabels_lines_already_on_screen(tmp_path):
    tui = _tui(tmp_path)
    _say(tui, 1, "B", "Walk me through the design.")
    rows = _transcript_rows(_render_text(tui))
    assert "Speaker B" in rows and "Sarah" not in rows
    tui._on_event({"type": "names", "names": {"B": "Sarah"}, "me": None,
                   "label": "B", "name": "Sarah", "source": "intro"})
    out = _render_text(tui)
    rows = _transcript_rows(out)
    assert "Speaker B" not in rows
    assert "Sarah" in rows and "Walk me through the design." in rows
    assert "Speaker B is Sarah" in out           # the status says where it came from


def test_marking_me_relabels_too(tmp_path):
    tui = _tui(tmp_path)
    _say(tui, 1, "A", "Hello there.")
    tui._on_event({"type": "me", "label": "A", "by": "name"})
    out = _render_text(tui)
    assert "Me " in _transcript_rows(out) and "Speaker A" not in _transcript_rows(out)
    assert "said your name" in out


def test_people_line_lists_me_and_every_voice(tmp_path):
    tui = _tui(tmp_path)
    for label in ("A", "B", "C", "D"):
        _say(tui, 1, label, f"line {label}")
    tui._on_event({"type": "names", "names": {"B": "Sarah"}, "me": "A"})
    header = _render_text(tui).splitlines()[2]
    assert "People" in header
    assert header.index("Me (A)") < header.index("Sarah (B)") < header.index("Speaker C") \
        < header.index("Speaker D")


def test_voice_colours_cycle_past_b_and_me_stays_green():
    from meeting_copilot.app import VOICE_COLOURS, voice_style
    styles = [voice_style(label, None) for label in "ABCDEFG"]
    assert len(set(styles)) == len(VOICE_COLOURS)       # A..G all distinct
    assert voice_style("A", None) == voice_style(chr(ord("A") + len(VOICE_COLOURS)), None)
    assert voice_style("C", "C") == "bold green"
    assert "green" not in "".join(styles)


def test_n_names_a_voice(tmp_path):
    tui = _tui(tmp_path)
    tui.engine.start_meeting = lambda: None
    tui.engine.session.start()
    for label, text in (("A", "Hi."), ("B", "Morning."), ("C", "Hello.")):
        tui.engine._on_final(text, label)        # the engine echoes utterance events to the TUI
    tui._on_event({"type": "me", "label": "A", "by": "key"})
    tui.engine.session.set_me("A")

    tui._command_key("n")
    assert tui.compose == "pick"
    footer = _render_text(tui).splitlines()[-2:]
    assert "Name which voice?" in footer[0] and " B  Speaker B" in footer[0]
    assert " A " not in footer[0]                # me is not offered
    tui._compose_key("b")                        # letters are case-insensitive
    assert tui.compose == "name" and tui.name_label == "B"
    for ch in "Sarah":
        tui._compose_key(ch)
    assert "name for Speaker B › Sarah" in _render_text(tui)
    tui._compose_key("\r")
    assert tui.compose is None
    assert tui.engine.session.names == {"B": "Sarah"}
    assert tui.engine.session.name_sources["B"] == "user"
    out = _render_text(tui)
    rows = _transcript_rows(out)
    assert "Sarah" in rows and "Morning." in rows and "Speaker B" not in rows
    assert "Speaker B is now Sarah." in out


def test_n_edit_prefills_and_empty_returns_to_automatic(tmp_path):
    tui = _tui(tmp_path)
    tui.engine.session.start()
    tui.engine._on_final("Hi, I'm Sarah.", "B")
    assert tui.names == {"B": "Sarah"}
    tui._command_key("n")
    tui._compose_key("B")
    assert tui.buffer == "Sarah" and tui.buffer_selected   # the current name, selected
    tui._compose_key("\r")                       # Enter confirms it as a typed name
    assert tui.engine.session.names == {"B": "Sarah"}
    assert tui.engine.session.name_sources == {"B": "user"}
    assert tui.status == "Speaker B is now Sarah."

    tui._command_key("n")
    tui._compose_key("B")
    for ch in "Priya":                           # typing replaces the selected name
        tui._compose_key(ch)
    tui._compose_key("\r")
    assert tui.engine.session.names == {"B": "Priya"}

    tui._command_key("n")
    tui._compose_key("B")
    tui._compose_key("\x7f")                     # Backspace clears the selection
    assert tui.buffer == ""
    tui._compose_key("\r")                       # empty: back to automatic
    assert tui.engine.session.names == {} and tui.names == {}
    out = _render_text(tui)
    assert "Speaker B" in out and "back to automatic" in out


def test_n_editing_after_the_first_key_appends(tmp_path):
    tui = _tui(tmp_path)
    tui.engine.session.start()
    tui.engine._on_final("Hi, I'm Sara.", "B")
    tui._command_key("n")
    tui._compose_key("B")
    tui._compose_key("\x7f")                     # clear, then type afresh
    for ch in "Sarahh":
        tui._compose_key(ch)
    tui._compose_key("\x7f")                     # now Backspace removes one character
    tui._compose_key("\r")
    assert tui.engine.session.names == {"B": "Sarah"}


def test_n_escape_and_wrong_letter_cancel(tmp_path):
    tui = _tui(tmp_path)
    tui.engine.session.start()
    tui.engine._on_final("Morning.", "B")
    tui._command_key("n")
    tui._compose_key("\x1b")                     # Esc at the letter prompt
    assert tui.compose is None
    tui._command_key("n")
    tui._compose_key("z")                        # nobody called Z
    assert tui.compose is None and "No voice Z" in tui.status
    tui._command_key("n")
    tui._compose_key("b")
    tui._compose_key("X")
    tui._compose_key("\x1b")                     # Esc while typing the name
    assert tui.compose is None and tui.engine.session.names == {}


def test_n_with_nobody_to_name(tmp_path):
    tui = _tui(tmp_path)
    tui._command_key("n")
    assert tui.compose is None and "Nobody to name yet" in tui.status


def test_m_cycles_through_the_voices_heard(tmp_path):
    tui = _tui(tmp_path)
    tui.engine.session.start()
    for label in ("B", "A", "C"):
        tui.engine._on_final("x", label)
    marked = []
    for _ in range(4):
        tui._command_key("m")
        marked.append(tui.me_label)
    assert marked == ["B", "A", "C", None]


# -- copy, supersede, mic -------------------------------------------------------------
def test_y_copies_the_last_finished_draft(tmp_path, monkeypatch):
    from meeting_copilot import clipboard
    tui = _tui(tmp_path)
    copied = []
    monkeypatch.setattr(clipboard, "copy", lambda text, write=None: copied.append(text) or "wl-copy")
    tui._command_key("y")
    assert copied == [] and "Nothing to copy" in tui.status
    tui._on_event({"type": "help", "question": "Q?", "answer": "First draft.", "served": "cli"})
    tui._on_event({"type": "help_started", "question": "Q2?", "note": "", "mode": "answer"})
    tui._on_event({"type": "help_delta", "text": "Half a sec"})   # the next one is streaming
    tui._command_key("y")
    tui._copy_thread.join(5)
    assert copied == ["First draft."]
    assert tui.status == "Copied the last draft (wl-copy)."


def test_y_over_ssh_writes_osc52_to_the_terminal(tmp_path, monkeypatch):
    import io
    from meeting_copilot import clipboard
    tui = _tui(tmp_path)
    tui.console = _console(file=io.StringIO(), width=80, height=24)
    monkeypatch.setattr(clipboard, "NATIVE", None)                      # not Windows
    monkeypatch.setattr(clipboard, "find_executable", lambda name: None)   # no tools
    tui._on_event({"type": "help", "question": "Q?", "answer": "Draft.", "served": "cli"})
    tui._command_key("y")
    tui._copy_thread.join(5)
    assert "OSC 52" in tui.status
    assert tui.console.file.getvalue() == clipboard.osc52("Draft.")


def test_a_second_help_says_the_first_was_dropped(tmp_path):
    tui = _tui(tmp_path)
    tui._on_event({"type": "help_started", "question": "Q?", "note": "", "mode": "answer"})
    tui._on_event({"type": "help_delta", "text": "Partly"})
    tui._on_event({"type": "help_started", "question": "Q?", "note": "", "mode": "answer"})
    assert "dropped" in tui.status and tui.answer is None and tui.thinking


def test_a_failed_draft_stops_the_spinner(tmp_path):
    tui = _tui(tmp_path)
    tui._on_event({"type": "help_started", "question": "Q?", "note": "", "mode": "answer"})
    tui._on_event({"type": "error", "msg": "assistant: boom", "help": True})
    assert not tui.thinking and not tui.drafting
    assert "Drafting your answer" not in _render_text(tui)


def test_level_events_never_wait_for_the_render_lock(tmp_path):
    tui = _tui(tmp_path)
    done = threading.Event()

    def capture_loop():
        tui._on_event({"type": "level", "on": True})
        done.set()

    with tui._lock:                              # the render loop is mid-frame
        threading.Thread(target=capture_loop, daemon=True).start()
        assert done.wait(timeout=2), "the capture loop blocked on the render lock"
    assert tui.mic_on is True
    tui._on_event({"type": "audio_stopped"})
    assert "mic off" in _render_text(tui)


def test_reads_at_80_by_24(tmp_path):
    tui = _tui(tmp_path)
    tui._on_event({"type": "state", "state": "recording"})
    for i, label in enumerate("ABC"):
        _say(tui, i, label, "A line long enough that it has to wrap onto a second row at "
                            "eighty columns, which is the narrow case.")
    tui._on_event({"type": "names", "names": {"B": "Sarah"}, "me": "A"})
    tui._on_event({"type": "help", "question": "Why?", "answer": "Because.", "served": "cli"})
    lines = _render_text(tui, width=80, height=24).splitlines()
    assert len(lines) == 24 and all(len(ln) <= 80 for ln in lines)
    text = "\n".join(lines)
    assert "REC" in text and "online" in text and "mic" in text
    assert "People" in text and "Sarah (B)" in text
    assert "Help: read this aloud" in text and "Live transcript" in text
    assert " h  help" in text and " q  quit" in text
    rows = _transcript_rows(text)
    assert "Speaker C" in rows and "which is the narrow case." in rows   # newest line shown


def test_the_newest_line_stays_visible_when_lines_wrap(tmp_path):
    tui = _tui(tmp_path)
    for i in range(40):
        _say(tui, i, "B", f"Utterance {i} is long enough to wrap across more than one row "
                          "of the transcript at this width, every single time.")
    tui._on_event({"type": "names", "names": {"B": "Sarah"}, "me": None})
    tui._on_event({"type": "partial", "speaker": "B", "text": "and one more"})
    rows = _transcript_rows(_render_text(tui, width=80, height=24))
    assert "Utterance 39" in rows
    assert "Sarah" in rows.splitlines()[-1] and "and one more" in rows.splitlines()[-1]


def test_a_new_meeting_starts_a_clean_transcript(tmp_path):
    tui = _tui(tmp_path)
    tui._on_event({"type": "state", "state": "recording"})
    _say(tui, 1, "B", "From the first meeting.")
    tui._on_event({"type": "state", "state": "ended"})
    tui._on_event({"type": "state", "state": "recording"})
    tui._on_event({"type": "names", "names": {}, "me": None})
    assert list(tui.lines) == [] and tui.voices == []
    assert "From the first meeting." not in _render_text(tui)


# -- the roster on screen ------------------------------------------------------------
def _roster_tui(tmp_path, people="Sarah Chen, Marcus Lee, Priya Raman"):
    eng = CopilotEngine(EngineConfig(root=tmp_path, my_name="Daniel", people=people))
    tui = CopilotTUI(eng, source_factory=lambda: None)
    eng.load_roster()
    eng.session.start()
    eng._on_final("Hi, I'm Daniel.", "A")        # me, by name
    return tui


def test_people_line_dims_roster_names_not_yet_placed(tmp_path):
    tui = _roster_tui(tmp_path)
    tui.engine._on_final("Hi everyone, I'm Sarah.", "B")
    header = _render_text(tui).splitlines()[2]
    assert "Sarah Chen (B)" in header
    tail = header[header.index("·"):]
    assert "Marcus Lee" in tail and "Priya Raman" in tail and "Sarah Chen" not in tail


def test_ready_status_names_the_roster_source(tmp_path):
    tui = _roster_tui(tmp_path)
    tui._on_event({"type": "ready"})
    assert "3 people from --people" in tui.status and "You are Daniel" in tui.status


def test_elimination_says_where_the_name_came_from(tmp_path):
    tui = _roster_tui(tmp_path, people="Sarah Chen")
    tui.engine._on_final("Morning.", "B")
    assert tui.names == {"B": "Sarah Chen"}
    assert "Speaker B is Sarah Chen, the one name left" in tui.status


def test_n_offers_roster_names_as_numbered_picks(tmp_path):
    tui = _roster_tui(tmp_path)
    tui.engine._on_final("Hi everyone, I'm Sarah.", "B")
    tui.engine._on_final("Hello.", "C")
    tui._command_key("n")
    tui._compose_key("c")
    footer = "\n".join(_render_text(tui, width=100).splitlines()[-3:])
    # unplaced names first, numbered; the one already placed comes last
    assert "or pick:" in footer
    assert footer.index(" 1  Marcus Lee") < footer.index(" 2  Priya Raman") \
        < footer.index(" 3  Sarah Chen")
    tui._compose_key("2")
    assert tui.compose is None
    assert tui.engine.session.names["C"] == "Priya Raman"
    assert tui.engine.session.name_sources["C"] == "user"


def test_n_typing_still_works_and_digits_after_typing_are_text(tmp_path):
    tui = _roster_tui(tmp_path)
    tui.engine._on_final("Hello.", "B")
    tui._command_key("n")
    tui._compose_key("b")
    for ch in "Team 2":
        tui._compose_key(ch)
    tui._compose_key("\r")
    assert tui.engine.session.names["B"] == "Team 2"
    tui._command_key("n")
    tui._compose_key("b")                        # prefilled "Team 2", selected
    tui._compose_key("9")                        # no ninth pick: typed, replacing it
    assert tui.compose == "name" and tui.buffer == "9"
    tui._compose_key("\x1b")


def test_n_picks_without_a_roster_are_absent(tmp_path):
    tui = _tui(tmp_path)
    tui.engine.session.start()
    tui.engine._on_final("Hello.", "B")
    tui._command_key("n")
    tui._compose_key("b")
    assert "or pick:" not in _render_text(tui)
    tui._compose_key("1")                        # with no roster a digit is just text
    assert tui.buffer == "1"


def test_header_forgets_me_when_a_new_meeting_starts(tmp_path):
    tui = _tui(tmp_path)
    tui.engine.cfg.stt_backend = "local"
    tui.engine._transcriber = object()                    # never used: no audio is fed
    tui.engine.start_meeting()
    tui.engine._on_final("Hello.", "A")
    tui.engine.cycle_me()
    assert "Me (A)" in _render_text(tui).splitlines()[2]
    tui.engine.end_meeting()
    tui.engine.start_meeting()
    header = _render_text(tui).splitlines()[2]
    assert tui.me_label is None and "Me (A)" not in header
    tui.engine.end_meeting()


# -- model and meeting text is shown as written, never parsed as markup ----------------
TRICKY = "Use [/x] and [bold]this[/bold], not :smile:"


def test_the_last_draft_prints_literally_on_exit(tmp_path):
    tui = _tui(tmp_path)
    tui.console = _console(width=100, height=20, record=True)
    tui.answer = TRICKY
    tui._farewell()                              # raised MarkupError when parsed as markup
    out = tui.console.export_text()
    assert TRICKY in out and "Sparky closed." in out


def test_tricky_text_renders_literally_everywhere(tmp_path):
    tui = _tui(tmp_path)
    _say(tui, 1, "B", TRICKY)
    tui._on_event({"type": "names", "names": {"B": "[red]Eve[/]"}, "me": None,
                   "roster": ["[/x] Mallory"], "label": "B", "name": "[red]Eve[/]",
                   "source": "intro"})
    tui._on_event({"type": "partial", "speaker": "B", "text": "[/oops"})
    tui._on_event({"type": "help_started", "question": TRICKY, "note": "[i]", "mode": "answer"})
    tui._on_event({"type": "help", "question": TRICKY, "answer": TRICKY, "served": "cli"})
    tui._on_event({"type": "error", "msg": "assistant: [/boom]"})
    out = _render_text(tui, width=120, height=30)
    assert TRICKY in out and "[red]Eve[/]" in out and "[/x] Mallory" in out
    assert "[/oops" in out and "Steer: [i]" in out and "assistant: [/boom]" in out


def test_a_slow_clipboard_never_blocks_the_keys(tmp_path, monkeypatch):
    import time as _time
    from meeting_copilot import clipboard
    tui = _tui(tmp_path)
    release = threading.Event()

    def slow_copy(text, write=None):
        release.wait(5)                          # a clipboard tool hanging on its timeout
        return "xclip"

    monkeypatch.setattr(clipboard, "copy", slow_copy)
    tui._on_event({"type": "help", "question": "Q?", "answer": "Draft.", "served": "cli"})
    began = _time.monotonic()
    tui._command_key("y")
    assert _time.monotonic() - began < 0.5       # the key thread is free at once
    assert tui.status == "Copying the last draft…"
    tui._command_key("y")                        # a second press while it runs
    assert tui.status == "Still copying the last draft…"
    tui._command_key("c")                        # other keys still work
    assert tui.compose == "note"
    release.set()
    tui._copy_thread.join(5)
    assert tui.status == "Copied the last draft (xclip)."
