"""Full-screen terminal UI for the meeting copilot.

A `rich` dashboard renders the live transcript while a raw-mode keyboard reader
on a side thread captures single keypresses (start / end / help / mark / quit).
All meeting control is by keypress — nothing is voice-activated. The UI only
reads state the engine publishes via events, so it never blocks the audio or
the answer-generation work, which run on their own threads.
"""

from __future__ import annotations

import select
import sys
import termios
import textwrap
import threading
import time
import tty
from collections import deque
from datetime import datetime

from rich.align import Align
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from .engine import CopilotEngine
from .session import fmt_clock

SPEAKER_STYLE = {"Me": "bold green",
                 "Speaker A": "bold cyan", "Speaker B": "bold magenta",
                 "Speaker ?": "white"}


class CopilotTUI:
    def __init__(self, engine: CopilotEngine, source_factory) -> None:
        self.engine = engine
        self.engine.on_event = self._on_event
        self.source_factory = source_factory
        self.console = Console()

        # Reentrant: key handlers call engine methods that emit events straight
        # back into _on_event, which re-acquires this lock on the same thread.
        self._lock = threading.RLock()
        self._quit = threading.Event()
        self.lines: deque[Text] = deque(maxlen=500)
        self.status = ""
        self.status_style = "dim"
        self.state = "idle"
        self.me_label: str | None = None
        # Which backend is currently in use (updated live on a mid-session switch).
        self.stt_active = "deepgram" if engine.cfg.stt_backend == "deepgram" else "local"
        self.answer_active = engine.answer_primary
        self.online = engine.online
        self.thinking = False
        self.answer: str | None = None
        self.answer_question: str | None = None
        self.answer_note: str = ""
        self.answer_mode = "answer"   # "answer" | "points": what the help box holds
        self.answer_scroll = 0   # line offset for scrolling a long answer
        self.partial: tuple[str, str] | None = None  # (speaker name, live text)
        self._start_mono: float | None = None
        # Compose box: type extra context for the next "help me!" request.
        self.compose = False
        self.note = ""          # pending note, consumed by the next 'h'
        self.note_buffer = ""   # what's being typed while composing

    # -- events from the engine -------------------------------------------
    def _on_event(self, e: dict) -> None:
        with self._lock:
            t = e["type"]
            if t == "utterance":
                ts = fmt_clock(e["t"])
                line = Text()
                line.append(f"[{ts}] ", style="dim")
                line.append(f"{e['name']}: ", style=SPEAKER_STYLE.get(e["name"], "white"))
                line.append(e["text"])
                self.lines.append(line)
                self.partial = None  # committed; clear the live line
            elif t == "partial":
                if e.get("text"):
                    self.partial = (e["name"], e["text"])
            elif t == "audio_stopped":
                self.partial = None
            elif t == "state":
                self.state = e["state"]
                if self.state == "recording":
                    self._start_mono = time.monotonic()
                    self._set_status("Recording. Press 'h' the moment you need something to say.", "green")
                elif self.state == "ended":
                    self._start_mono = None
            elif t == "me":
                self.me_label = e["label"]
            elif t == "help_started":
                self.thinking = True
                self.answer = None
                self.answer_scroll = 0   # start each answer from the top
                self.answer_question = e["question"]
                self.answer_note = e.get("note", "")
                self.answer_mode = e.get("mode", "answer")
                self._set_status("Drafting talking points…" if self.answer_mode == "points"
                                 else "Thinking…", "yellow")
            elif t == "help_delta":
                self.thinking = False
                self.answer = e["text"]  # streaming: grows token by token
            elif t == "help":
                self.thinking = False
                self.answer_question = e["question"]
                self.answer = e["answer"]
                self.answer_mode = e.get("mode", self.answer_mode)
                self.answer_active = e.get("served", self.answer_active)
                what = "Talking points" if self.answer_mode == "points" else "Answer"
                self._set_status(f"{what} ready ({self.answer_active}). Read it out.", "bold green")
            elif t == "model":
                self._set_status(f"Answer model: {e['model']}", "bold cyan")
            elif t == "answer_switch":   # a backend failed; the chain moves to the next
                self.answer = None       # discard any partial stream from the failed one
                self._set_status(f"Answers: {e.get('failed','?')} failed, trying the next backend", "bold yellow")
            elif t == "stt_switch":      # Deepgram dropped -> local Whisper
                self.stt_active = e.get("backend", "local")
                self.partial = None
                self._set_status(f"Transcription: Deepgram dropped, now local Whisper ({e.get('reason','')})", "bold yellow")
            elif t == "connectivity":
                self.online = e["online"]
                if self.online:
                    self._set_status("Back online: cloud backends available again.", "bold green")
                else:
                    self._set_status("Offline: using local Whisper + local LLM.", "bold yellow")
            elif t == "exported":
                self._set_status(f"Saved transcript to {e['path']}", "bold green")
            elif t == "info":
                self._set_status(e["msg"], "dim")
            elif t == "ready":
                self._set_status("Ready. Press 's' to start the meeting.", "bold")
            elif t == "error":
                self._set_status(e["msg"], "bold red")

    def _set_status(self, msg: str, style: str = "dim") -> None:
        self.status = msg
        self.status_style = style

    # -- rendering ---------------------------------------------------------
    def _header(self) -> Panel:
        badge = {"idle": "[bold white on grey23] IDLE ",
                 "recording": "[bold white on red] ● REC ",
                 "ended": "[bold white on grey23] ENDED "}.get(self.state, self.state)
        elapsed = fmt_clock(time.monotonic() - self._start_mono) if self._start_mono else "00:00"
        me = {None: "unset", "A": "Speaker A", "B": "Speaker B"}.get(self.me_label, str(self.me_label))
        cfg = self.engine.cfg

        net = "[green]● online[/]" if self.online else "[bold red]● OFFLINE[/]"

        stt_pref = "deepgram" if cfg.stt_backend == "deepgram" else "local"
        stt_disp = {"deepgram": f"deepgram:{cfg.deepgram_model}",
                    "local": f"whisper:{cfg.whisper_model}"}[self.stt_active]
        stt_tag = stt_disp if self.stt_active == stt_pref else f"[bold yellow]{stt_disp} (fallback)[/]"

        ans_pref = self.engine.answer_primary
        ans_model = cfg.ollama_model if self.answer_active == "local" else cfg.answer_model
        ans_disp = f"{ans_model}·{self.answer_active}"
        ans_tag = ans_disp if self.answer_active == ans_pref else f"[bold yellow]{ans_disp} (fallback)[/]"

        head = Text.from_markup(
            f"{badge}[/]  {net}  {elapsed}  "
            f"stt {stt_tag}  llm {ans_tag}  me={me}"
        )
        return Panel(head, title="Sparky", border_style="yellow")

    def _transcript(self, height: int) -> Panel:
        rows: list[Text] = list(self.lines)
        if self.partial is not None:
            name, text = self.partial
            live = Text()
            live.append(f"{name}: ", style="italic " + SPEAKER_STYLE.get(name, "white"))
            live.append(text + " ▌", style="italic dim")
            rows.append(live)
        body = rows[-(height):] if height > 0 else rows
        if body:
            content: Group | Align = Group(*body)
        else:
            content = Align.center(Text("…waiting for speech…", style="dim"), vertical="middle")
        return Panel(content, title="Live transcript", border_style="cyan")

    def _answer_lines(self, width: int) -> list[Text]:
        """Wrap the Q/note/answer into a flat list of styled lines for scrolling."""
        out: list[Text] = []
        if self.answer_question:
            lead = "From: " if self.answer_mode == "points" else "Q: "
            for ln in textwrap.wrap(lead + self.answer_question, width) or [""]:
                out.append(Text(ln, style="dim italic"))
        if self.answer_note:
            for ln in textwrap.wrap("Steer: " + self.answer_note, width) or [""]:
                out.append(Text(ln, style="cyan italic"))
        out.append(Text(""))
        for para in (self.answer or "").split("\n"):
            if not para.strip():
                out.append(Text(""))
                continue
            for ln in textwrap.wrap(para, width):
                out.append(Text(ln, style="bold white"))
        return out

    def _panel_title(self) -> str:
        return ("Talking points: pick one and say it" if self.answer_mode == "points"
                else "Help: read this aloud")

    def _answer_panel(self, lines: list[Text] | None, inner_height: int) -> Panel:
        if lines is None:  # nothing drafted yet
            if self.thinking:
                drafting = ("Drafting talking points…" if self.answer_mode == "points"
                            else "Drafting your answer…")
                inner: Group | Align = Align.center(Text(drafting, style="yellow"), vertical="middle")
                border = "yellow"
            else:
                inner = Align.center(
                    Text("'h' drafts what to say next: an answer if you were just asked "
                         "something, talking points otherwise.",
                         style="dim"), vertical="middle")
                border = "grey39"
            return Panel(inner, title=self._panel_title(), border_style=border)

        total = len(lines)
        start = self.answer_scroll
        end = min(total, start + inner_height)
        visible = lines[start:end] or [Text("")]
        title = self._panel_title()
        if total > inner_height:  # scrollable: show position + hint
            title += f"   [{start + 1}-{end}/{total}] ↑/↓ scroll"
        border = "yellow" if self.thinking else "green"
        return Panel(Group(*visible), title=title, border_style=border)

    def _footer(self) -> Panel:
        if self.compose:
            line = Text()
            line.append("context › ", style="bold cyan")
            line.append(self.note_buffer + "▌")
            line.append("    (Enter save · Esc cancel)", style="dim")
            return Panel(line, title="Add context for next help", border_style="cyan")

        keys = ("[bold]s[/] start   [bold]e[/] end+save   [bold]h[/] help!   "
                "[bold]c[/] add context   [bold]m[/] mark me   "
                "[bold]1[/]haiku [bold]2[/]sonnet [bold]3[/]opus   [bold]q[/] quit")
        rows: list[Text] = [Text.from_markup(keys)]
        if self.note:
            note_line = Text()
            note_line.append("context queued: ", style="cyan")
            note_line.append(self.note, style="italic cyan")
            rows.append(note_line)
        rows.append(Text(self.status, style=self.status_style))
        return Panel(Group(*rows), border_style="blue")

    def _render(self) -> Layout:
        H, W = self.console.size.height, self.console.size.width
        avail = max(6, H - 8)              # rows left after header(3) + footer(5)
        width = max(20, W - 4)             # inner text width of the answer panel

        # The answer panel grows to fit its content (so it isn't capped), capped
        # so the transcript keeps at least 3 rows; overflow beyond that scrolls.
        lines = self._answer_lines(width) if self.answer else None
        if lines is not None:
            answer_h = max(6, min(len(lines) + 2, avail - 3))
            inner = answer_h - 2
            self.answer_scroll = max(0, min(self.answer_scroll, max(0, len(lines) - inner)))
        else:
            answer_h = max(5, min(9, avail - 3))
            inner = answer_h - 2
        transcript_h = avail - answer_h

        # The help box (the answer you read aloud) sits ABOVE the transcript so
        # the thing you act on is at eye level, not buried under the scrolling log.
        layout = Layout()
        layout.split_column(
            Layout(self._header(), size=3, name="header"),
            Layout(self._answer_panel(lines, inner), size=answer_h, name="answer"),
            Layout(self._transcript(transcript_h - 2), size=transcript_h, name="transcript"),
            Layout(self._footer(), size=5, name="footer"),
        )
        return layout

    # -- keyboard ----------------------------------------------------------
    def _keyboard_loop(self) -> None:
        fd = sys.stdin.fileno()
        old = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while not self._quit.is_set():
                ch = sys.stdin.read(1)
                if not ch:
                    continue
                if ch == "\x1b":  # Esc, or the lead-in of an arrow/page key
                    if select.select([sys.stdin], [], [], 0.04)[0]:
                        seq = sys.stdin.read(2)            # e.g. "[A", "[B", "[5"
                        if seq in ("[5", "[6") and select.select([sys.stdin], [], [], 0.01)[0]:
                            sys.stdin.read(1)              # consume trailing "~"
                        self._handle_arrow(seq)
                        continue
                    # lone Esc falls through (compose cancel)
                # Don't hold the render lock across dispatch: handlers call into
                # the engine (which may block on a socket connect and emits events
                # that re-enter the lock). The handlers only touch simple state.
                if self.compose:
                    self._compose_key(ch)
                else:
                    self._command_key(ch)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old)

    def _handle_arrow(self, seq: str) -> None:
        # Scroll the answer; the down direction is clamped to content in _render.
        if seq == "[A":      # Up
            self.answer_scroll = max(0, self.answer_scroll - 1)
        elif seq == "[B":    # Down
            self.answer_scroll += 1
        elif seq == "[5":    # PageUp
            self.answer_scroll = max(0, self.answer_scroll - 8)
        elif seq == "[6":    # PageDown
            self.answer_scroll += 8

    def _compose_key(self, ch: str) -> None:
        if ch in ("\r", "\n"):                    # save the note, leave compose
            self.note = self.note_buffer.strip()
            self.compose = False
            self._set_status("Context saved. Press 'h' to use it.", "cyan")
        elif ch == "\x1b":                         # Esc cancels the edit
            self.compose = False
            self.note_buffer = ""
        elif ch in ("\x7f", "\b"):                 # Backspace
            self.note_buffer = self.note_buffer[:-1]
        elif ch == "\x03":                         # Ctrl-C
            self._quit.set()
        elif ch.isprintable():
            self.note_buffer += ch

    def _command_key(self, ch: str) -> None:
        low = ch.lower()
        if low == "q":
            self._quit.set()
        elif low == "s":
            if self.state != "recording":
                # Threaded: opening the Deepgram stream can block ~6s.
                threading.Thread(target=self.engine.start_meeting, daemon=True).start()
        elif low == "e":
            if self.state == "recording":
                threading.Thread(target=self.engine.end_meeting, daemon=True).start()
        elif low == "h":
            note, self.note = self.note, ""        # consume the queued note (one-shot)
            threading.Thread(target=self.engine.request_help, args=(note,),
                             kwargs={"mode": "auto"}, daemon=True).start()
        elif low == "c":
            self.compose = True
            self.note_buffer = self.note           # edit any existing note
        elif low == "m":
            self.engine.cycle_me()
        elif ch in ("1", "2", "3"):
            self.engine.set_answer_model({"1": "haiku", "2": "sonnet", "3": "opus"}[ch])

    # -- main loop ---------------------------------------------------------
    def run(self) -> None:
        self.console.print("[bold]Preparing…[/] gathering context and loading the model.")
        self.engine.prepare()

        audio = threading.Thread(
            target=self.engine.run, args=(self.source_factory(),), daemon=True)
        audio.start()
        keys = threading.Thread(target=self._keyboard_loop, daemon=True)
        keys.start()

        try:
            with Live(self._render(), console=self.console, screen=True,
                      refresh_per_second=8, redirect_stderr=False) as live:
                while not self._quit.is_set():
                    with self._lock:
                        live.update(self._render())
                    time.sleep(0.12)
        finally:
            if self.state == "recording":
                self.engine.end_meeting()
            self.engine.stop()

        # Leave the user with the last answer + where the transcript went.
        if self.answer:
            self.console.print(Panel(self.answer, title="Last drafted answer", border_style="green"))
        self.console.print("[dim]Sparky closed.[/]")
