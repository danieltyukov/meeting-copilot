import threading

from rich.console import Console

from meeting_copilot.app import CopilotTUI
from meeting_copilot.engine import CopilotEngine, EngineConfig


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
    tui.console = Console(width=90, height=44, record=True)
    tui.answer_question = "Why this design?"
    tui.answer = _long_answer(8)
    tui.console.print(tui._render())
    out = tui.console.export_text()
    assert "Paragraph number 7" in out      # last paragraph visible — not capped
    assert "↑/↓ scroll" not in out           # no scrolling needed


def test_long_answer_scrolls_in_short_window(tmp_path):
    tui = _tui(tmp_path)
    tui.answer_question = "Why this design?"
    tui.answer = _long_answer(20)

    def render():
        tui.console = Console(width=90, height=15, record=True)
        tui.console.print(tui._render())
        return tui.console.export_text()

    top = render()
    assert "↑/↓ scroll" in top                            # capped -> scrollable
    assert "Paragraph number 0" in top
    assert "Paragraph number 19" not in top               # bottom hidden
    for _ in range(80):
        tui._handle_arrow("[B")                           # scroll to the end
    bottom = render()
    assert "Paragraph number 19" in bottom                # now visible


def test_help_box_renders_above_transcript(tmp_path):
    """The answer/help box sits ABOVE the live transcript, not below it."""
    tui = _tui(tmp_path)

    def render():
        tui.console = Console(width=90, height=44, record=True)
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
    tui.console = Console(width=130, height=30, record=True)
    return tui


def test_header_normal_when_no_switch(tmp_path):
    tui = _tui_api_deepgram(tmp_path)
    tui.console.print(tui._render())
    out = tui.console.export_text()
    assert "deepgram:nova-3" in out and "sonnet·api" in out
    assert "fallback" not in out


def test_header_shows_fallback_tags_after_switch(tmp_path):
    tui = _tui_api_deepgram(tmp_path)
    tui._on_event({"type": "stt_switch", "backend": "local", "reason": "dropped"})
    tui._on_event({"type": "help", "question": "Q?", "answer": "A", "served": "cli"})
    tui.console.print(tui._render())
    out = tui.console.export_text()
    assert "whisper:base (fallback)" in out      # STT switched, shown in header
    assert "cli (fallback)" in out               # answers served by CLI, shown in header
    assert tui.stt_active == "local" and tui.answer_active == "cli"


def test_header_online_offline_marker(tmp_path):
    tui = _tui_api_deepgram(tmp_path)
    tui.console.print(tui._render())
    assert "online" in tui.console.export_text()

    tui._on_event({"type": "connectivity", "online": False})
    assert tui.online is False
    tui.console = Console(width=130, height=30, record=True)
    tui.console.print(tui._render())
    assert "OFFLINE" in tui.console.export_text()


def test_header_local_answer_shows_ollama_model(tmp_path):
    tui = _tui_api_deepgram(tmp_path)
    tui._on_event({"type": "help", "question": "Q?", "answer": "A", "served": "local"})
    tui.console.print(tui._render())
    out = tui.console.export_text()
    # header shows the configured local LLM (the Ollama default) tagged ·local
    assert f"{tui.engine.cfg.ollama_model}·local" in out


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
        tui.console = Console(width=90, height=30, record=True)
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
    tui.console = Console(width=120, height=30, record=True)
    tui.console.print(tui._render())
    out = tui.console.export_text()          # export clears the record: read it once
    assert "h help!" in out and "t points" not in out
