"""Meeting state, transcript storage and export.

This module is deliberately free of audio, ML or LLM concerns so the core
bookkeeping can be unit-tested without a microphone. The engine feeds it
finished utterances; the TUI reads from it to render; on stop it serialises the
whole conversation to a Markdown file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path

# A far-end line that reads as a question: it ends in "?" or opens the way a
# spoken question does. Used to skip backchannels ("Right, yes.") when picking
# what to answer.
QUESTION_RE = re.compile(
    r"\?\s*$|^(?:so[,\s]+)?(?:and[,\s]+)?(?:who|what|when|where|why|how|which|whose|can|could|"
    r"would|should|do|does|did|is|are|was|were|will|have|has|tell me|walk me|talk me|"
    r"describe|explain)\b", re.IGNORECASE)
# How many far-end lines back a question is still worth answering; a run is the
# tail of consecutive lines from one voice (a lead-in plus the question itself).
QUESTION_LOOKBACK = 8
RUN_MAX = 3


class State(Enum):
    IDLE = "idle"
    RECORDING = "recording"
    ENDED = "ended"


@dataclass
class Utterance:
    t: float  # seconds since meeting start
    speaker: str  # raw cluster label: "A", "B" or "?"
    text: str


@dataclass
class Assist:
    t: float
    question: str      # the question answered, or the line the points continue from
    answer: str
    kind: str = "answer"   # "answer" | "points"


def fmt_clock(seconds: float) -> str:
    seconds = max(0, int(seconds))
    return f"{seconds // 60:02d}:{seconds % 60:02d}"


class Session:
    """Owns the conversation: state, utterances, and who 'me' is."""

    def __init__(self) -> None:
        self.state = State.IDLE
        self.utterances: list[Utterance] = []
        self.assists: list[Assist] = []
        self.me_label: str | None = None  # which cluster label ("A"/"B") is me
        self.started_wall: datetime | None = None
        self.ended_wall: datetime | None = None

    # -- state transitions -------------------------------------------------
    @property
    def is_recording(self) -> bool:
        return self.state is State.RECORDING

    def start(self, now: datetime | None = None) -> None:
        self.state = State.RECORDING
        self.utterances = []
        self.assists = []
        self.started_wall = now or datetime.now()
        self.ended_wall = None

    def end(self, now: datetime | None = None) -> None:
        if self.state is State.RECORDING:
            self.ended_wall = now or datetime.now()
        self.state = State.ENDED

    # -- ingest ------------------------------------------------------------
    def add_utterance(self, t: float, speaker: str, text: str) -> Utterance | None:
        text = text.strip()
        if not self.is_recording or not text:
            return None
        utt = Utterance(t=t, speaker=speaker, text=text)
        self.utterances.append(utt)
        return utt

    def add_assist(self, t: float, question: str, answer: str, kind: str = "answer") -> None:
        self.assists.append(Assist(t=t, question=question, answer=answer, kind=kind))

    # -- speaker naming ----------------------------------------------------
    def set_me(self, label: str | None) -> None:
        self.me_label = label

    def speaker_name(self, label: str) -> str:
        if label == "?":
            return "Speaker ?"
        if label == self.me_label:
            return "Me"
        # Everyone who isn't me keeps their own letter, so a meeting with several
        # other voices stays legible instead of collapsing into one name.
        return f"Speaker {label}"

    # -- queries -----------------------------------------------------------
    def recent(self, n: int) -> list[Utterance]:
        return self.utterances[-n:]

    def last_line(self) -> Utterance | None:
        """The newest utterance from anyone: where the conversation is right now."""
        return self.utterances[-1] if self.utterances else None

    def _far_end_since_me(self) -> list[Utterance]:
        """Far-end lines since I last spoke, newest first (all far-end lines when
        I have not spoken or am not marked), capped to the lookback window."""
        out: list[Utterance] = []
        for utt in reversed(self.utterances):
            if self.me_label is not None and utt.speaker == self.me_label:
                break
            out.append(utt)
            if len(out) >= QUESTION_LOOKBACK:
                break
        return out

    def decide_help(self) -> tuple[str, str]:
        """What one keypress should draft, from the transcript alone.

        ``("answer", question)`` when the far end has put a question-shaped line
        to me since I last spoke; otherwise ``("points", last line)``: I just
        spoke, or they only acknowledged, or nothing has been said yet, so the
        useful thing is what to say next. The anchor is empty before anyone speaks.
        """
        if any(QUESTION_RE.search(u.text) for u in self._far_end_since_me()):
            q = self.latest_question()
            return "answer", (q.text if q else "")
        last = self.last_line()
        return "points", (last.text if last else "")

    def _is_far_end(self, utt: Utterance) -> bool:
        return self.me_label is None or utt.speaker != self.me_label

    def latest_question(self) -> Utterance | None:
        """The far-end line worth answering right now.

        The newest run of lines from one far-end voice is the default (joined,
        so a lead-in and its question arrive together). When that run is just a
        backchannel, the most recent question-shaped line a few lines back wins
        instead. Falls back to the most recent utterance overall when speakers
        are unknown or everything is attributed to me.
        """
        if not self.utterances:
            return None
        far = [u for u in self.utterances if self._is_far_end(u)]
        if not far:
            return self.utterances[-1]

        # The tail run: consecutive far-end lines from the same voice, newest last.
        run = [far[-1]]
        for utt in reversed(far[:-1]):
            if len(run) >= RUN_MAX or utt.speaker != run[0].speaker:
                break
            if self.utterances.index(utt) != self.utterances.index(run[0]) - 1:
                break                        # something (my reply) sits in between
            run.insert(0, utt)
        joined = Utterance(t=run[0].t, speaker=run[0].speaker, text=" ".join(u.text for u in run))
        if any(QUESTION_RE.search(u.text) for u in run):
            return joined
        for utt in reversed(far[-QUESTION_LOOKBACK:-len(run)]):
            if QUESTION_RE.search(utt.text):
                return utt
        return joined

    def transcript_text(self, with_speakers: bool = True) -> str:
        lines = []
        for utt in self.utterances:
            if with_speakers:
                lines.append(f"[{fmt_clock(utt.t)}] {self.speaker_name(utt.speaker)}: {utt.text}")
            else:
                lines.append(utt.text)
        return "\n".join(lines)

    # -- export ------------------------------------------------------------
    def export_markdown(self, context_dir: Path | str) -> str:
        context_dir = Path(context_dir)
        started = self.started_wall or datetime.now()
        ended = self.ended_wall or datetime.now()
        duration = (ended - started).total_seconds()

        out: list[str] = []
        out.append(f"# Meeting transcript — {context_dir.name}")
        out.append("")
        out.append(f"- **Directory:** `{context_dir}`")
        out.append(f"- **Started:** {started.strftime('%Y-%m-%d %H:%M:%S')}")
        out.append(f"- **Ended:** {ended.strftime('%Y-%m-%d %H:%M:%S')}")
        out.append(f"- **Duration:** {fmt_clock(duration)}")
        out.append(f"- **Utterances:** {len(self.utterances)}")
        out.append("")
        out.append("## Conversation")
        out.append("")
        if self.utterances:
            for utt in self.utterances:
                out.append(f"**[{fmt_clock(utt.t)}] {self.speaker_name(utt.speaker)}:** {utt.text}")
                out.append("")
        else:
            out.append("_(no speech captured)_")
            out.append("")

        if self.assists:
            out.append("## Copilot assists")
            out.append("")
            for a in self.assists:
                if a.kind == "points":
                    out.append(f"### [{fmt_clock(a.t)}] Talking points")
                    out.append("")
                    if a.question:
                        out.append(f"> continuing from: {a.question}")
                        out.append("")
                    out.append("**Talking points:**")
                else:
                    out.append(f"### [{fmt_clock(a.t)}] Question")
                    out.append("")
                    out.append(f"> {a.question}")
                    out.append("")
                    out.append("**Drafted answer:**")
                out.append("")
                out.append(a.answer)
                out.append("")

        return "\n".join(out).rstrip() + "\n"

    def write_export(self, context_dir: Path | str) -> Path:
        context_dir = Path(context_dir)
        stamp = (self.started_wall or datetime.now()).strftime("%Y%m%d-%H%M%S")
        dest = context_dir / f"meeting-{stamp}.md"
        dest.write_text(self.export_markdown(context_dir), encoding="utf-8")
        return dest
