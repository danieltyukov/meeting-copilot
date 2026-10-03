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

from .names import detect_name, same_name
from .roster import eliminate as eliminate_by_roster
from .roster import match_roster, name_key, roster_others, unused_roster

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
    speaker: str  # raw cluster label: "A", "B", ... or "?"
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


def display_name(label: str, me_label: str | None, names: dict[str, str]) -> str:
    """How a voice is labelled: Me, its name when known, else ``Speaker X``.
    Shared by the session and the TUI so both resolve labels the same way."""
    if label == "?":
        return "Speaker ?"
    if label == me_label:
        return "Me"
    # Everyone who isn't me keeps their own letter until they are named, so a
    # meeting with several other voices stays legible instead of collapsing.
    return names.get(label) or f"Speaker {label}"


class Session:
    """Owns the conversation: state, utterances, who 'me' is and who the others are."""

    def __init__(self, my_name: str | None = None) -> None:
        self.state = State.IDLE
        self.utterances: list[Utterance] = []
        self.assists: list[Assist] = []
        self.me_label: str | None = None  # which cluster label ("A", "B", ...) is me
        self.my_name = (my_name or "").strip() or None
        self.names: dict[str, str] = {}         # label -> name
        # label -> where the name came from, strongest first: "user" (typed),
        # "intro" (they said it), "roster" (the one name left over)
        self.name_sources: dict[str, str] = {}
        self.roster: list[str] = []   # the other people in the meeting (invite, --people)
        # Voices whose eliminated name I cleared: elimination leaves them alone,
        # so a wrong guess does not snap straight back.
        self.declined: set[str] = set()
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
        # Cluster labels restart with every stream (a new diarizer or Deepgram
        # connection), so neither names nor which letter is me carry over.
        self.me_label = None
        self.names = {}
        self.name_sources = {}
        self.declined = set()
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
        return display_name(label, self.me_label, self.names)

    def heard_labels(self) -> list[str]:
        """Every voice that has said something final, first heard first ("?" aside)."""
        seen: list[str] = []
        for utt in self.utterances:
            if utt.speaker != "?" and utt.speaker not in seen:
                seen.append(utt.speaker)
        return seen

    def observe_name(self, utt: Utterance) -> list[tuple[str, str]]:
        """Name voices from a final line: its speaker's introduction, then
        elimination against the roster. Returns what changed as ``(label,
        source)`` pairs, source being "intro", "me" or "roster".

        An intro names a voice that has no name yet or only a roster one; a
        typed name or an earlier intro is never replaced. The name is matched
        to the roster ("Sarah" becomes "Sarah Chen"). When it is my name and
        nobody is marked as me yet, that voice is me instead.
        """
        changes: list[tuple[str, str]] = []
        label = utt.speaker
        if (label != "?" and label != self.me_label
                and self.name_sources.get(label) not in ("user", "intro")):
            name = detect_name(utt.text)
            if name and self.my_name and same_name(name, self.my_name):
                # My name from a second voice is my own voice split in two: ignore it.
                if self.me_label is None:
                    self.me_label = label
                    if self.name_sources.get(label) == "roster":
                        self._unname(label)
                    changes.append((label, "me"))
            elif name:
                self._claim(label, match_roster(name, self.roster), "intro")
                changes.append((label, "intro"))
        return changes + self.eliminate()

    def eliminate(self) -> list[tuple[str, str]]:
        """Give the one unnamed voice the one roster name left, when that is
        certain. Needs me marked: the room mic hears me too, so until then a
        voice cannot be counted as one of the others."""
        if self.me_label is None or not self.roster:
            return []
        voices = [v for v in self.heard_labels() if v != self.me_label]
        found = eliminate_by_roster(voices, {v: self.names[v] for v in voices if v in self.names},
                                    self.roster)
        found = {label: name for label, name in found.items() if label not in self.declined}
        for label, name in found.items():
            self.names[label] = name
            self.name_sources[label] = "roster"
        return [(label, "roster") for label in found]

    def set_roster(self, names) -> list[tuple[str, str]]:
        """The people in the meeting, from the invite or --people. I am left
        out by name. Returns any voice that elimination names right away."""
        self.roster = roster_others(self.my_name, names)
        return self.eliminate()

    def rename(self, label: str, name: str) -> bool:
        """Name a voice by hand. A typed name always wins over detection; an
        empty one returns the voice to automatic (a later intro can name it).
        Returns whether anything changed."""
        name = " ".join(name.split())
        if label == "?":
            return False
        if not name:
            if label not in self.names:
                return False
            if self.name_sources.get(label) == "roster":
                self.declined.add(label)
            self._unname(label)
            return True
        if self.names.get(label) == name and self.name_sources.get(label) == "user":
            return False
        # The same name as a guess or an intro still counts: typing it confirms it.
        self._claim(label, name, "user")
        return True

    def _claim(self, label: str, name: str, source: str) -> None:
        """Name a voice. Another voice that holds the same person only by
        elimination was a guess that is now wrong, so it goes back to automatic."""
        key = name_key(match_roster(name, self.roster))
        for other in [v for v, s in self.name_sources.items() if s == "roster" and v != label]:
            if name_key(self.names[other]) == key:
                self._unname(other)
        self.names[label] = name
        self.name_sources[label] = source
        # A name from me or from the voice settles what clearing a guess was about.
        self.declined.discard(label)

    def _unname(self, label: str) -> None:
        self.names.pop(label, None)
        self.name_sources.pop(label, None)

    def unplaced_roster(self) -> list[str]:
        """Roster names no voice other than mine has yet."""
        return unused_roster([n for v, n in self.names.items() if v != self.me_label],
                             self.roster)

    def participants(self) -> list[str]:
        """Me first, then every other voice heard, by name or number, then the
        people on the roster no voice has been matched to."""
        me = f"Me ({self.my_name})" if self.my_name else "Me"
        out: dict[str, str] = {}     # one entry per person, even when two voices share a name
        for label in self.heard_labels():
            if label != self.me_label:
                name = self.speaker_name(label)
                out.setdefault(name_key(name), name)
        return [me] + list(out.values()) + self.unplaced_roster()

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
        out.append(f"# Meeting transcript: {context_dir.name}")
        out.append("")
        out.append(f"- **Directory:** `{context_dir}`")
        out.append(f"- **Started:** {started.strftime('%Y-%m-%d %H:%M:%S')}")
        out.append(f"- **Ended:** {ended.strftime('%Y-%m-%d %H:%M:%S')}")
        out.append(f"- **Duration:** {fmt_clock(duration)}")
        out.append(f"- **Utterances:** {len(self.utterances)}")
        out.append(f"- **Participants:** {', '.join(self.participants())}")
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
