"""Full-screen terminal UI for the meeting copilot.

A `rich` dashboard renders the live transcript while a raw-mode keyboard reader
on a side thread captures single keypresses (start / end / help / name / mark /
quit). All meeting control is by keypress: nothing is voice-activated. The UI
only reads state the engine publishes via events, so it never blocks the audio
or the answer-generation work, which run on their own threads.
"""

from __future__ import annotations

import textwrap
import threading
import time
from collections import deque

from rich import box
from rich.align import Align
from rich.cells import cell_len
from rich.console import Console, Group
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from . import __version__, clipboard
from .engine import ANSWER_MODELS, CopilotEngine
from .keys import NAMED, read_keys
from .roster import unused_roster
from .session import display_name, fmt_clock

ACCENT = "#FFC61A"          # Sparky yellow, the brand colour
ME_STYLE = "bold green"
# One colour per voice letter, cycling past the end: A cyan, B magenta, C blue...
VOICE_COLOURS = ["cyan", "magenta", "#60A5FA", "#F4A261", "#A78BFA", "#2DD4BF", "#F472B6"]
CAP_STYLE = "bold #E6EDF3 on #30363D"
CAP_PRIMARY = "bold #0D1117 on " + ACCENT
NAME_COL_MAX = 16           # widest speaker column in the transcript
NAME_LEN_MAX = 40           # longest name you can type


def voice_style(label: str | None, me_label: str | None) -> str:
    """The colour a voice is drawn in: green for me, a stable colour per letter."""
    if label is None or label == "?":
        return "bold white"
    if label == me_label:
        return ME_STYLE
    if len(label) == 1 and "A" <= label <= "Z":
        i = ord(label) - ord("A")
    elif label.isdigit():
        i = int(label)
    else:
        i = sum(map(ord, label))
    return "bold " + VOICE_COLOURS[i % len(VOICE_COLOURS)]


def key_cap(key: str, label: str = "", primary: bool = False) -> Text:
    t = Text()
    t.append(f" {key} ", style=CAP_PRIMARY if primary else CAP_STYLE)
    if label:
        t.append(f" {label}", style="grey70")
    return t


def pack(items: list[Text], width: int, gap: int = 2) -> list[Text]:
    """Lay key hints out in as few lines as fit ``width``, never splitting one."""
    lines: list[Text] = []
    cur: Text | None = None
    for item in items:
        if cur is not None and cur.cell_len + gap + item.cell_len <= width:
            cur.append(" " * gap)
            cur.append_text(item)
            continue
        if cur is not None:
            lines.append(cur)
        cur = item.copy()
    if cur is not None:
        lines.append(cur)
    return lines


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
        # (t, label, text): labels resolve to names when drawn, so naming a
        # voice relabels every line it already spoke.
        self.lines: deque[tuple[float, str, str]] = deque(maxlen=500)
        self.voices: list[str] = []          # labels heard, first heard first
        self.names: dict[str, str] = {}
        self.roster: list[str] = []          # the other people in the meeting
        self.status = ""
        self.status_style = "dim"
        self.state = "idle"
        self.me_label: str | None = None
        # Which backend is currently in use (updated live on a mid-session switch).
        self.stt_active = "deepgram" if engine.cfg.stt_backend == "deepgram" else "local"
        self.answer_active = engine.answer_primary
        self.online = engine.online
        self.mic_on = False      # speech heard right now (the header's mic dot)
        self.mic_dead = False    # the audio source stopped
        self.thinking = False
        self.drafting = False    # from help_started until the draft lands or fails
        self.answer: str | None = None
        self.answer_question: str | None = None
        self.answer_note: str = ""
        self.answer_mode = "answer"   # "answer" | "points": what the help box holds
        self.answer_scroll = 0   # line offset for scrolling a long answer
        self.last_draft: str | None = None   # what 'y' copies
        self._copy_thread: threading.Thread | None = None
        self.partial: tuple[str | None, str] | None = None  # (label, live text)
        self._start_mono: float | None = None
        # The footer prompt: None, "note" (context for the next help), "pick"
        # (which voice to name) or "name" (typing that voice's name).
        self.compose: str | None = None
        self.note = ""          # pending note, consumed by the next 'h'
        self.buffer = ""        # what's being typed in the prompt
        # A prefilled name acts like selected text: the first key replaces it.
        self.buffer_selected = False
        self.name_label: str | None = None   # the voice being named

    # -- events from the engine -------------------------------------------
    def _on_event(self, e: dict) -> None:
        t = e["type"]
        if t == "level":
            # From the capture loop, which must never wait on the render lock;
            # a bool assignment needs no lock.
            self.mic_on = e["on"]
            return
        with self._lock:
            if t == "utterance":
                label = e.get("speaker") or "?"
                self.lines.append((e["t"], label, e["text"]))
                if label != "?" and label not in self.voices:
                    self.voices.append(label)
                self.partial = None  # committed; clear the live line
            elif t == "partial":
                if e.get("text"):
                    self.partial = (e.get("speaker"), e["text"])
            elif t == "audio_stopped":
                self.partial = None
                self.mic_on = False
                self.mic_dead = True
            elif t == "state":
                self.state = e["state"]
                if self.state == "recording":
                    self._start_mono = time.monotonic()
                    # Labels restart with each meeting, and the last one is saved.
                    self.lines.clear()
                    self.voices = []
                    self._set_status("Recording. Press 'h' the moment you need something to say.", "green")
                elif self.state == "ended":
                    self._start_mono = None
            elif t == "names":
                self.names = dict(e.get("names") or {})
                self.me_label = e.get("me", self.me_label)
                self.roster = list(e.get("roster", self.roster))
                label, source = e.get("label"), e.get("source")
                if label and source == "intro":
                    self._set_status(f"Speaker {label} is {e['name']}, from their intro. "
                                     "'n' renames.", "cyan")
                elif label and source == "roster":
                    self._set_status(f"Speaker {label} is {e['name']}, the one name left. "
                                     "'n' renames.", "cyan")
                elif label and source == "user":
                    self._set_status(f"Speaker {label} is now {e['name']}.", "cyan")
                elif label and source is None:
                    self._set_status(f"Speaker {label} is back to automatic naming.", "cyan")
            elif t == "me":
                self.me_label = e["label"]
                if e.get("by") == "name":
                    self._set_status(f"Speaker {self.me_label} said your name, so that voice "
                                     "is you now. 'm' changes it.", "bold green")
                elif self.me_label is not None:
                    self._set_status(f"Speaker {self.me_label} is you now.", "green")
                elif self.voices:
                    self._set_status("No voice marked as you.", "dim")
                else:
                    self._set_status("Nobody heard yet: speak first, then press 'm'.", "dim")
            elif t == "help_started":
                if self.drafting:
                    self._set_status("Drafting again; the earlier draft was dropped.", "yellow")
                else:
                    self._set_status("Drafting talking points…" if e.get("mode") == "points"
                                     else "Thinking…", "yellow")
                self.thinking = True
                self.drafting = True
                self.answer = None
                self.answer_scroll = 0   # start each answer from the top
                self.answer_question = e["question"]
                self.answer_note = e.get("note", "")
                self.answer_mode = e.get("mode", "answer")
            elif t == "help_delta":
                self.thinking = False
                self.answer = e["text"]  # streaming: grows token by token
            elif t == "help":
                self.thinking = False
                self.drafting = False
                self.answer_question = e["question"]
                self.answer = e["answer"]
                self.last_draft = e["answer"]
                self.answer_mode = e.get("mode", self.answer_mode)
                self.answer_active = e.get("served", self.answer_active)
                what = "Talking points" if self.answer_mode == "points" else "Answer"
                self._set_status(f"{what} ready ({self.answer_active}). Read it out, "
                                 "or 'y' to copy.", "bold green")
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
                you = f" You are {self.engine.cfg.my_name}." if self.engine.cfg.my_name else ""
                who = ""
                if self.roster and self.engine.roster_source:
                    n = len(self.roster)
                    who = (f" {n} {'person' if n == 1 else 'people'} from "
                           f"{self.engine.roster_source}.")
                self._set_status(f"Ready. Press 's' to start the meeting.{you}{who}", "bold")
            elif t == "error":
                if e.get("help"):
                    self.thinking = False
                    self.drafting = False
                self._set_status(e["msg"], "bold red")

    def _set_status(self, msg: str, style: str = "dim") -> None:
        self.status = msg
        self.status_style = style

    def _name(self, label: str | None) -> str:
        return display_name(label, self.me_label, self.names) if label else "…"

    def _nameable(self) -> list[str]:
        """Voices 'n' can name: heard, one letter, and not me."""
        return [v for v in self.voices if len(v) == 1 and v != self.me_label]

    def _unplaced(self) -> list[str]:
        """Roster names that no voice other than mine has yet."""
        return unused_roster([n for v, n in self.names.items() if v != self.me_label],
                             self.roster)

    def _picks(self) -> list[str]:
        """Roster names offered as 1-9 when naming a voice: the unplaced ones first."""
        unplaced = self._unplaced()
        return (unplaced + [r for r in self.roster if r not in unplaced])[:9]

    # -- rendering ---------------------------------------------------------
    def _stt_words(self) -> tuple[str, bool]:
        cfg = self.engine.cfg
        words = (f"Deepgram {cfg.deepgram_model}" if self.stt_active == "deepgram"
                 else f"Whisper {cfg.whisper_model}")
        preferred = "deepgram" if cfg.stt_backend == "deepgram" else "local"
        return words, self.stt_active != preferred

    def _llm_words(self) -> tuple[str, bool]:
        cfg = self.engine.cfg
        if self.answer_active == "local":
            words = f"{cfg.ollama_model} via Ollama"
        else:
            model = cfg.answer_model or "Claude"
            model = model.capitalize() if model in ANSWER_MODELS else model
            words = f"{model} via {'API' if self.answer_active == 'api' else 'CLI'}"
        return words, self.answer_active != self.engine.answer_primary

    def _people(self) -> Text:
        line = Text()
        line.append("People  ", style="grey50")
        line.append("Me", style=ME_STYLE)
        if self.me_label:
            line.append(f" ({self.me_label})", style="grey50")
        others = [v for v in self.voices if v != self.me_label]
        for label in others:
            name = self._name(label)
            line.append("   ")
            line.append(name, style=voice_style(label, self.me_label))
            if label in self.names:
                line.append(f" ({label})", style="grey50")
        unplaced = self._unplaced()
        if unplaced:                      # in the meeting, not matched to a voice yet
            line.append("   ·   ", style="grey35")
            line.append("   ".join(unplaced), style="italic grey50")
        elif not others:
            line.append("   nobody else heard yet", style="grey50")
        line.no_wrap = True
        line.overflow = "ellipsis"
        return line

    def _header(self) -> Panel:
        pill = {"idle": (" IDLE ", "bold white on grey30"),
                "recording": (" ● REC ", "bold white on #D93636"),
                "ended": (" ENDED ", "bold white on grey30")}.get(self.state, (f" {self.state} ", ""))
        elapsed = fmt_clock(time.monotonic() - self._start_mono) if self._start_mono else "00:00"

        left = Text()
        left.append(*pill)
        left.append(f"  {elapsed}", style="bold")
        left.append("   ")
        if self.online:
            left.append("● ", style="green")
            left.append("online", style="grey70")
        else:
            left.append("● OFFLINE", style="bold red")
        left.append("   ")
        if self.mic_dead:
            left.append("● mic off", style="bold red")
        else:
            left.append("● ", style="bold bright_green" if self.mic_on else "grey35")
            left.append("mic", style="grey70")

        right = Text()
        for i, (words, fallback) in enumerate((self._stt_words(), self._llm_words())):
            if i:
                right.append("  ·  ", style="grey50")
            if fallback:
                right.append(f"{words} (fallback)", style="bold yellow")
            else:
                right.append(words, style="grey70")

        right.no_wrap = True
        right.overflow = "ellipsis"
        top = Table.grid(expand=True, padding=(0, 0, 0, 2))
        # The state cluster never shrinks; the backend names give way first.
        top.add_column(no_wrap=True, min_width=left.cell_len)
        top.add_column(justify="right")     # collapsible; the text itself never wraps
        top.add_row(left, right)
        title = Text.assemble(("Sparky", f"bold {ACCENT}"), (f" {__version__}", "grey50"))
        return Panel(Group(top, self._people()), title=title, title_align="left",
                     box=box.ROUNDED, border_style=ACCENT)

    def _transcript_lines(self, width: int, height: int) -> list[Text]:
        """The newest ``height`` screen lines of the transcript: time, speaker
        column, then the text wrapped under itself."""
        rows: list[tuple[str, str | None, str, bool]] = [
            (fmt_clock(t), label, text, False) for t, label, text in
            list(self.lines)[-max(1, height):]]
        if self.partial is not None:
            label, text = self.partial
            rows.append(("", label, text, True))
        if not rows:
            return []
        name_w = min(NAME_COL_MAX, max(cell_len(self._name(r[1])) for r in rows))
        body_w = max(10, width - 7 - name_w - 2)
        out: list[Text] = []
        for clock, label, text, live in reversed(rows):
            name = self._name(label)
            if cell_len(name) > name_w:
                name = name[:name_w - 1] + "…"
            style = voice_style(label, self.me_label)
            body = Text(text + (" ▌" if live else ""), style="italic grey62" if live else "")
            wrapped = body.wrap(self.console, body_w)
            block: list[Text] = []
            for i, piece in enumerate(wrapped):
                line = Text(no_wrap=True)
                line.append(f"{clock:<5}  " if i == 0 else " " * 7, style="grey50")
                line.append((name if i == 0 else "").ljust(name_w) + "  ",
                            style=("italic " + style) if live else style)
                line.append_text(piece)
                block.append(line)
            out[:0] = block
            if len(out) >= height:
                break
        return out[-height:]

    def _transcript(self, height: int, width: int) -> Panel:
        body = self._transcript_lines(width, height)
        if body:
            content: Group | Align = Group(*body)
        else:
            hint = ("Listening…" if self.state == "recording"
                    else "Press 's' to start the meeting.")
            content = Align.center(Text(hint, style="grey50"), vertical="middle")
        return Panel(content, title=Text("Live transcript", style="bold grey82"), title_align="left",
                     box=box.ROUNDED, border_style="grey35")

    def _answer_lines(self, width: int) -> list[Text]:
        """Wrap the Q/note/answer into a flat list of styled lines for scrolling."""
        out: list[Text] = []
        if self.answer_question:
            lead = "From: " if self.answer_mode == "points" else "Q: "
            for ln in textwrap.wrap(lead + self.answer_question, width) or [""]:
                out.append(Text(ln, style="grey62 italic"))
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
        title = Text(self._panel_title(), style="bold")
        if lines is None:  # nothing drafted yet
            if self.thinking:
                drafting = ("Drafting talking points…" if self.answer_mode == "points"
                            else "Drafting your answer…")
                inner: Group | Align = Align.center(Text(drafting, style=ACCENT), vertical="middle")
                border = ACCENT
            else:
                inner = Align.center(
                    Text("'h' drafts what to say next: an answer if you were just asked "
                         "something, talking points otherwise.",
                         style="grey50"), vertical="middle")
                border = "grey35"
            return Panel(inner, title=title, title_align="left", box=box.ROUNDED,
                         border_style=border)

        total = len(lines)
        start = self.answer_scroll
        end = min(total, start + inner_height)
        visible = lines[start:end] or [Text("")]
        if total > inner_height:  # scrollable: show position + hint
            title.append(f"   [{start + 1}-{end}/{total}] ↑/↓ scroll", style="grey50")
        border = ACCENT if self.thinking or self.drafting else "green"
        return Panel(Group(*visible), title=title, title_align="left", box=box.ROUNDED,
                     border_style=border)

    def _keys(self) -> list[Text]:
        rec = self.state == "recording"
        return [key_cap("e", "end") if rec else key_cap("s", "start"),
                key_cap("h", "help", primary=True), key_cap("y", "copy"),
                key_cap("c", "note"), key_cap("n", "name"), key_cap("m", "me"),
                key_cap("1-3", "model"), key_cap("q", "quit")]

    def _footer_rows(self, width: int) -> list[Text]:
        rows: list[Text] = []
        if self.compose == "pick":
            line = Text("Name which voice?  ", style=f"bold {ACCENT}")
            line.append_text(Text("  ").join(
                key_cap(v, self._name(v)) for v in self._nameable()))
            rows.append(line)
            rows.append(Text("Press its letter. Esc cancels.", style="grey50"))
        elif self.compose in ("note", "name"):
            if self.compose == "note":
                prompt, hint = "note › ", "Enter saves, Esc cancels. It steers the next 'h'."
            else:
                prompt = f"name for Speaker {self.name_label} › "
                hint = "Enter saves, Esc cancels, empty means automatic."
            line = Text(prompt, style=f"bold {ACCENT}")
            line.append(self.buffer, style="reverse" if self.buffer_selected else "")
            line.append("▌")
            rows.append(line)
            picks = self._picks() if self.compose == "name" else []
            if picks:
                choose = Text("or pick: ", style="grey50")
                choose.append_text(Text("  ").join(
                    key_cap(str(i), name) for i, name in enumerate(picks, 1)))
                rows.append(choose)
            rows.append(Text(hint, style="grey50"))
        else:
            rows.append(Text(self.status, style=self.status_style))
            if self.note:
                note_line = Text("note queued: ", style="cyan")
                note_line.append(self.note, style="italic cyan")
                rows.append(note_line)
            rows.extend(pack(self._keys(), width))
        for row in rows:
            row.no_wrap = True
            row.overflow = "ellipsis"
        return rows

    def _render(self) -> Layout:
        H, W = self.console.size.height, self.console.size.width
        footer = self._footer_rows(W)
        header_h = 4
        avail = max(9, H - header_h - len(footer))
        width = max(20, W - 4)             # inner text width of the answer panel

        # The answer panel grows to fit its content (so it isn't capped), capped
        # so the transcript keeps at least 2 lines; overflow beyond that scrolls.
        lines = self._answer_lines(width) if self.answer else None
        if lines is not None:
            answer_h = max(6, min(len(lines) + 2, avail - 4))
            inner = answer_h - 2
            self.answer_scroll = max(0, min(self.answer_scroll, max(0, len(lines) - inner)))
        else:
            answer_h = 5
            inner = answer_h - 2
        transcript_h = avail - answer_h

        # The help box (the answer you read aloud) sits ABOVE the transcript so
        # the thing you act on is at eye level, not buried under the scrolling log.
        layout = Layout()
        layout.split_column(
            Layout(self._header(), size=header_h, name="header"),
            Layout(self._answer_panel(lines, inner), size=answer_h, name="answer"),
            Layout(self._transcript(transcript_h - 2, width), size=transcript_h, name="transcript"),
            Layout(Group(*footer), size=len(footer), name="footer"),
        )
        return layout

    # -- keyboard ----------------------------------------------------------
    def _keyboard_loop(self) -> None:
        for key in read_keys(self._quit):
            if key in NAMED:
                self._handle_arrow(key)
                continue
            # Don't hold the render lock across dispatch: handlers call into
            # the engine (which may block on a socket connect and emits events
            # that re-enter the lock). The handlers only touch simple state.
            if self.compose:
                self._compose_key(key)
            else:
                self._command_key(key)

    def _handle_arrow(self, key: str) -> None:
        # Scroll the answer; the down direction is clamped to content in _render.
        if key == "up":
            self.answer_scroll = max(0, self.answer_scroll - 1)
        elif key == "down":
            self.answer_scroll += 1
        elif key == "pgup":
            self.answer_scroll = max(0, self.answer_scroll - 8)
        elif key == "pgdn":
            self.answer_scroll += 8

    def _compose_key(self, ch: str) -> None:
        if ch == "\x03":                           # Ctrl-C
            self._quit.set()
        elif self.compose == "pick":
            self._pick_key(ch)
        elif ch in ("\r", "\n"):                   # save, leave the prompt
            self._submit()
        elif ch == "\x1b":                         # Esc cancels the edit
            self.compose = None
            self.buffer = ""
            self.buffer_selected = False
            self.name_label = None
        elif self._roster_pick(ch):
            self.buffer = self._roster_pick(ch)       # saved at once
            self._submit()
        elif ch in ("\x7f", "\b"):                 # Backspace
            self.buffer = "" if self.buffer_selected else self.buffer[:-1]
            self.buffer_selected = False
        elif ch.isprintable():
            if self.buffer_selected:
                self.buffer, self.buffer_selected = "", False
            if self.compose != "name" or len(self.buffer) < NAME_LEN_MAX:
                self.buffer += ch

    def _roster_pick(self, ch: str) -> str | None:
        """The roster name a digit picks while naming a voice. Only before any
        typing: once a name is being typed, digits are part of it."""
        if self.compose != "name" or len(ch) != 1 or not "1" <= ch <= "9":
            return None
        if self.buffer and not self.buffer_selected:
            return None
        picks = self._picks()
        return picks[int(ch) - 1] if int(ch) <= len(picks) else None

    def _pick_key(self, ch: str) -> None:
        label = ch.upper()
        if ch == "\x1b" or not ch.isprintable():
            self.compose = None
            self._set_status("Naming cancelled.", "dim")
        elif label in self._nameable():
            self.compose = "name"
            self.name_label = label
            self.buffer = self.names.get(label, "")   # shown selected: typing replaces it
            self.buffer_selected = bool(self.buffer)
        else:
            self.compose = None
            self._set_status(f"No voice {label} to name. Press 'n' to try again.", "yellow")

    def _submit(self) -> None:
        mode, text = self.compose, self.buffer.strip()
        self.compose = None
        self.buffer = ""
        self.buffer_selected = False
        if mode == "note":
            self.note = text
            self._set_status("Context saved. Press 'h' to use it." if text
                             else "Context cleared.", "cyan")
        elif mode == "name" and self.name_label:
            label, self.name_label = self.name_label, None
            # The engine reports a change with a names event, which replaces this.
            self._set_status(f"Speaker {label}: no change.", "dim")
            self.engine.rename_speaker(label, text)

    def _copy_draft(self) -> None:
        """Copy on a side thread: each clipboard tool tried may take up to 2 s,
        and the key thread must stay free. The result lands in the status line."""
        if not self.last_draft:
            self._set_status("Nothing to copy yet: press 'h' for a draft first.", "dim")
            return
        if self._copy_thread is not None and self._copy_thread.is_alive():
            self._set_status("Still copying the last draft…", "dim")
            return
        self._set_status("Copying the last draft…", "dim")
        self._copy_thread = threading.Thread(target=self._copy, args=(self.last_draft,),
                                             daemon=True)
        self._copy_thread.start()

    def _copy(self, text: str) -> None:
        how = clipboard.copy(text, write=self._write_raw)
        with self._lock:
            if how == "osc52":
                self._set_status("Sent the draft to the terminal clipboard (OSC 52). "
                                 "If paste is empty, your terminal blocks it.", "cyan")
            else:
                self._set_status(f"Copied the last draft ({how}).", "cyan")

    def _write_raw(self, seq: str) -> None:
        # Rich writes frames from the Live refresh thread; holding the console's
        # lock keeps the escape from landing in the middle of one.
        with self.console._lock:
            self.console.file.write(seq)
            self.console.file.flush()

    def _command_key(self, ch: str) -> None:
        low = ch.lower()
        if low == "q" or ch == "\x03":           # Ctrl-C arrives as a key on Windows
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
        elif low == "y":
            self._copy_draft()
        elif low == "c":
            self.compose = "note"
            self.buffer = self.note                # edit any existing note
        elif low == "n":
            if self._nameable():
                self.compose = "pick"
            else:
                self._set_status("Nobody to name yet." if not self.voices else
                                 "Only your voice so far, nobody else to name.", "dim")
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

        self._farewell()

    def _farewell(self) -> None:
        """Leave the user with the last answer once the screen is gone."""
        if self.answer:
            # Text, not a markup string: a draft can contain "[/x]" or ":smile:".
            self.console.print(Panel(Text(self.answer), title="Last drafted answer",
                                     box=box.ROUNDED, border_style="green"))
        self.console.print("[dim]Sparky closed.[/]")
