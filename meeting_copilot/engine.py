"""Headless orchestration: audio -> backend (transcribe/diarize) -> session.

The engine reads frames from an audio source and forwards them to a pluggable
transcription backend. The backend reports partial (live) and final utterances
through callbacks; the engine commits finals to the session and emits events for
the UI. Because forwarding is cheap, the capture loop never blocks, which is
what keeps the whole stream alive (the original bug was transcribing inline).
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .assistant import (DEFAULT_OLLAMA_MODEL, ApiAssistant, AssistantError,
                        ChainAssistant, CliAssistant, OllamaAssistant)
from .audio import LevelGate, UtteranceSegmenter, frame_rms
from .backends import Backend, DeepgramBackend, FallbackSttBackend, LocalBackend
from .context import gather_context
from .diarize import Diarizer
from .net import ConnectivityMonitor
from .roster import find_invite, ics_files, parse_ics, parse_people
from .session import Session
from .transcribe import Transcriber

Event = dict
EventCb = Callable[[Event], None]


@dataclass
class EngineConfig:
    root: Path
    stt_backend: str = "deepgram"          # "deepgram" | "local"
    stt_fallback: bool = True              # Deepgram -> local Whisper on failure
    deepgram_api_key: str | None = None
    deepgram_model: str = "nova-3"
    whisper_model: str = "base"            # used only by the local backend
    answer_model: str | None = "sonnet"
    answer_effort: str = "low"
    answer_backend: str = "auto"           # "auto" | "api" | "cli"
    anthropic_api_key: str | None = None
    ollama_model: str = DEFAULT_OLLAMA_MODEL   # local LLM for offline answers
    ollama_host: str = "http://localhost:11434"
    diarize: bool = True
    language: str | None = "en"
    context_budget: int = 60000
    my_name: str | None = None             # --me / MY_NAME: marks my voice, steers drafts
    people: str | None = None              # --people "Sarah Chen, Marcus Lee"
    invite: Path | None = None             # --invite FILE.ics (else the one .ics in root)


ANSWER_MODELS = ["haiku", "sonnet", "opus"]


class CopilotEngine:
    def __init__(self, config: EngineConfig, on_event: EventCb | None = None) -> None:
        self.cfg = config
        self.on_event = on_event or (lambda e: None)
        self.session = Session(my_name=config.my_name)
        self.monitor = ConnectivityMonitor(on_change=self._on_connectivity_change)
        # A newer request_help supersedes one still streaming: each draft carries
        # the generation it started under, and only the newest may emit. The
        # thread-local lets the chain's switch callback know whose draft it is in.
        self._help_gen = 0
        self._help_lock = threading.RLock()   # makes "is it current? then emit" atomic
        self._help_local = threading.local()
        self.assistant, self.answer_primary = self._build_assistant(config)
        self.assistant.set_my_name(config.my_name or "")
        self.roster_source: str | None = None   # where the roster came from, for the UI
        self.context: str = ""
        self.backend: Backend | None = None
        self._transcriber: Transcriber | None = None  # lazily built for local
        self._transcriber_lock = threading.Lock()
        self.stt_mode = self._stt_mode_label(config)
        self._start_mono: float | None = None
        self._stop = threading.Event()
        self._lock = threading.RLock()

    @staticmethod
    def _stt_mode_label(config: EngineConfig) -> str:
        if config.stt_backend == "deepgram":
            return "deepgram→local" if config.stt_fallback else "deepgram"
        return "local"

    # -- assistant construction -------------------------------------------
    def _build_assistant(self, config: EngineConfig):
        """Build the connectivity-aware answer chain: API → CLI → local LLM.
        The local backend keeps answers working with no internet (if Ollama is
        installed). Returns (chain, primary_label)."""
        key = config.anthropic_api_key or os.environ.get("ANTHROPIC_API_KEY")
        use_api = config.answer_backend in ("auto", "api") and bool(key)
        backends = []
        if use_api:
            backends.append(("api", ApiAssistant(api_key=key, model=config.answer_model), True))
        backends.append(
            ("cli", CliAssistant(model=config.answer_model, effort=config.answer_effort), True))
        backends.append(
            ("local", OllamaAssistant(model=config.ollama_model, host=config.ollama_host), False))
        chain = ChainAssistant(
            backends, is_online=self.monitor.is_online,
            on_switch=lambda name, reason: self._emit_for_draft(
                getattr(self._help_local, "gen", None), "answer_switch", failed=name, reason=reason))
        return chain, ("api" if use_api else "cli")

    @property
    def online(self) -> bool:
        return self.monitor.is_online()

    def _on_connectivity_change(self, online: bool) -> None:
        self._emit("connectivity", online=online)
        if not online and isinstance(self.backend, FallbackSttBackend):
            # WiFi dropped mid-session — switch transcription to local now.
            self.backend.force_local("network offline")

    # -- local STT (lazy, pre-warmed for the fallback) --------------------
    def _ensure_transcriber(self) -> Transcriber:
        with self._transcriber_lock:
            if self._transcriber is None:
                t = Transcriber(model_size=self.cfg.whisper_model, language=self.cfg.language)
                t.load()
                self._transcriber = t
        return self._transcriber

    def _prewarm_local(self) -> None:
        def warm():
            try:
                self._ensure_transcriber()
            except Exception as exc:
                self._emit("info", msg=f"(local STT fallback unavailable: {exc})")
        threading.Thread(target=warm, daemon=True).start()

    def _make_local_backend(self) -> LocalBackend:
        return LocalBackend(
            transcriber=self._ensure_transcriber(),
            diarizer=Diarizer(enabled=self.cfg.diarize),
            segmenter_factory=UtteranceSegmenter,
        )

    # -- backend construction ---------------------------------------------
    def _make_backend(self) -> Backend:
        if self.cfg.stt_backend != "deepgram":
            return self._make_local_backend()
        if not self.online and self.cfg.stt_fallback:
            # Offline: don't waste time on a doomed Deepgram connect.
            self._emit("stt_switch", backend="local", reason="offline at start")
            return self._make_local_backend()
        primary = DeepgramBackend(
            api_key=self.cfg.deepgram_api_key or "",
            model=self.cfg.deepgram_model,
            language=self.cfg.language or "en",
            diarize=self.cfg.diarize,
            on_error=lambda msg: self._emit("error", msg=msg),
        )
        if not self.cfg.stt_fallback:
            return primary
        return FallbackSttBackend(
            primary,
            local_factory=self._make_local_backend,
            on_switch=lambda backend, reason: self._emit(
                "stt_switch", backend=backend, reason=reason),
        )

    # -- lifecycle ---------------------------------------------------------
    def _emit(self, type_: str, **kw) -> None:
        self.on_event({"type": type_, **kw})

    def _emit_for_draft(self, gen: int | None, type_: str, **kw) -> bool:
        """Emit only while ``gen`` is the newest draft; a superseded one stays quiet."""
        with self._help_lock:
            if gen is not None and gen != self._help_gen:
                return False
            self._emit(type_, **kw)
            return True

    def _emit_names(self, label: str | None = None, source: str | None = None) -> None:
        with self._lock:
            names, me = dict(self.session.names), self.session.me_label
            roster = list(self.session.roster)
            name = self.session.speaker_name(label) if label else None
        self._emit("names", names=names, me=me, roster=roster, label=label, name=name,
                   source=source)

    def _emit_changes(self, changes: list[tuple[str, str]]) -> None:
        for label, source in changes:
            if source == "me":
                self._emit("me", label=label, by="name")
            self._emit_names(label, source)

    def load_roster(self) -> None:
        """Who else is in the meeting: --people, --invite, or else the single
        .ics invite at the root of the launch directory. The names go into the
        drafts and the saved participants, and name voices by elimination."""
        names: list[str] = []
        sources: list[str] = []
        if self.cfg.people:
            names += parse_people(self.cfg.people)
            sources.append("--people")
        invite = self.cfg.invite
        found = False
        if invite is None and not self.cfg.people:
            invite = find_invite(self.cfg.root)
            found = invite is not None
            if invite is None and len(ics_files(self.cfg.root)) > 1:
                self._emit("info", msg="Several .ics files here, so no invite was used. "
                                       "Pass --invite FILE to pick one.")
        if invite is not None:
            try:
                names += parse_ics(Path(invite).read_text(encoding="utf-8", errors="replace"))
                sources.append(Path(invite).name)
            except OSError as exc:
                self._emit("error", msg=f"Could not read the invite {invite}: {exc}")
        with self._lock:
            changes = self.session.set_roster(names)
            roster = list(self.session.roster)
        self.assistant.set_people(roster)
        self.roster_source = " and ".join(sources) or None
        if found and roster:
            self._emit("info", msg=f"Using the invite {Path(invite).name} from this directory: "
                                   f"{', '.join(roster)}.")
        elif roster:
            self._emit("info", msg=f"People in this meeting: {', '.join(roster)}.")
        elif found:
            self._emit("info", msg=f"The invite {Path(invite).name} lists nobody besides you.")
        self._emit_names()
        self._emit_changes(changes)

    def prepare(self) -> None:
        self._emit("info", msg=f"Reading context of {self.cfg.root} ...")
        self.context = gather_context(self.cfg.root, self.cfg.context_budget)
        self.monitor.start()
        self._emit("connectivity", online=self.online)
        self.load_roster()
        if self.cfg.stt_backend == "local":
            self._emit("info", msg=f"Loading Whisper '{self.cfg.whisper_model}' ...")
            self._ensure_transcriber()
        else:
            self._emit("info", msg="Using Deepgram streaming transcription.")
            if self.cfg.stt_fallback:
                # Warm the local model in the background so a Deepgram drop can
                # switch over with little gap.
                self._prewarm_local()
        self._emit("ready")

    def _elapsed(self) -> float:
        return (time.monotonic() - self._start_mono) if self._start_mono else 0.0

    # -- meeting control (keypress-driven) -------------------------------
    def start_meeting(self) -> None:
        try:
            with self._lock:
                backend = self._make_backend()   # may raise (e.g. missing key)
            backend.start(self._on_partial, self._on_final)
        except Exception as exc:
            self._emit("error", msg=f"Could not start transcription: {exc}")
            return
        with self._lock:
            self.backend = backend
            self.session.start()
            self._start_mono = time.monotonic()
        self._emit("state", state=self.session.state.value)
        self._emit_names()   # a new meeting starts with nobody named

    def end_meeting(self) -> Path | None:
        with self._lock:
            was_recording = self.session.is_recording
            backend = self.backend
        # Drain the backend FIRST, while the session is still recording, so any
        # in-flight final results (the last thing said) still commit.
        if backend is not None and was_recording:
            backend.finish()
        with self._lock:
            self.backend = None
            self.session.end()
        self._emit("state", state=self.session.state.value)
        if not was_recording:
            return None
        return self._safe_export()

    def _safe_export(self) -> Path | None:
        """Write the transcript, falling back to a writable location so a bad
        launch dir (deleted, read-only, full) never loses the transcript."""
        seen: list[Path] = []
        candidates = [self.cfg.root, Path.home(), Path(tempfile.gettempdir())]
        last_err: Exception | None = None
        for target in candidates:
            if target in seen:
                continue
            seen.append(target)
            try:
                path = self.session.write_export(target)
            except OSError as exc:
                last_err = exc
                continue
            self._emit("exported", path=str(path), utterances=len(self.session.utterances))
            return path
        self._emit("error", msg=f"Could not save transcript: {last_err}")
        return None

    def cycle_me(self) -> None:
        """Mark the next voice heard as me: none, then each voice in the order
        they first spoke, then none again."""
        with self._lock:
            order = [None, *self.session.heard_labels()]
            cur = self.session.me_label
            nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else order[-1]
            self.session.set_me(nxt)
            changes = self.session.eliminate()   # knowing who I am may settle the last voice
        self._emit("me", label=nxt, by="key")
        self._emit_names(nxt, "me")
        self._emit_changes(changes)

    def rename_speaker(self, label: str, name: str) -> None:
        """Name a voice by hand; an empty name returns it to automatic."""
        with self._lock:
            changed = self.session.rename(label, name)
            source = self.session.name_sources.get(label)
        if changed:
            self._emit_names(label, source)

    def set_answer_model(self, model: str) -> None:
        self.cfg.answer_model = model
        self.assistant.set_model(model)
        self._emit("model", model=model)

    def cycle_answer_model(self) -> None:
        cur = self.cfg.answer_model
        idx = ANSWER_MODELS.index(cur) if cur in ANSWER_MODELS else -1
        self.set_answer_model(ANSWER_MODELS[(idx + 1) % len(ANSWER_MODELS)])

    # -- backend callbacks -------------------------------------------------
    def _on_partial(self, text: str, speaker: str | None) -> None:
        with self._lock:
            if not self.session.is_recording:
                return
            name = self.session.speaker_name(speaker) if speaker else "…"
        self._emit("partial", speaker=speaker, name=name, text=text)

    def _on_final(self, text: str, speaker: str | None) -> None:
        with self._lock:
            if not self.session.is_recording:
                return
            utt = self.session.add_utterance(self._elapsed(), speaker or "?", text)
            if utt is None:
                return
            # Names come from finals only: a partial is a guess that may change.
            changes = self.session.observe_name(utt)
            name = self.session.speaker_name(utt.speaker)
        self._emit_changes(changes)
        self._emit("utterance", t=utt.t, speaker=utt.speaker, name=name, text=utt.text)

    # -- pipeline ----------------------------------------------------------
    def run(self, source) -> None:
        """Blocking capture loop. Run in a background thread.

        Besides forwarding frames it keeps a level gate for the mic dot, which
        shows whether the room is being heard even before the meeting starts.
        The gate only reports changes, a few times a second at most."""
        gate = LevelGate()
        try:
            with source as src:
                for frame in src.frames():
                    if self._stop.is_set():
                        break
                    if self.session.is_recording and self.backend is not None:
                        self.backend.feed(frame)
                    on = gate.update(frame_rms(frame))
                    if on is not None:
                        self._emit("level", on=on)
        except Exception as exc:
            self._emit("error", msg=f"audio: {exc}")
        if gate.state:
            self._emit("level", on=False)
        self._emit("audio_stopped")

    def stop(self) -> None:
        self._stop.set()
        self.monitor.stop()

    # -- help --------------------------------------------------------------
    def request_help(self, note: str = "", mode: str = "auto") -> None:
        """Draft something for me to say, streaming it as it generates.

        ``mode`` is ``auto`` (the default: the session decides from the
        transcript), ``answer`` (reply to the latest question; needs one) or
        ``points`` (talking points to carry the conversation on from the last
        thing said; works even before anyone has spoken, as openers). ``note``
        is optional extra context the user typed.

        A newer request supersedes one still streaming: the older one runs to
        the end on its own thread, but nothing it produces reaches the UI or
        the saved assists.
        """
        with self._lock:
            if mode == "auto":
                mode, question = self.session.decide_help()
            elif mode == "points":
                anchor = self.session.last_line()
                question = anchor.text if anchor else ""
            else:
                latest = self.session.latest_question()
                question = latest.text if latest else None
            transcript = self.session.transcript_text()
        if question is None:
            self._emit("info", msg="No question captured yet. Start the meeting first.")
            return
        with self._help_lock:
            self._help_gen += 1
            gen = self._help_gen
            self._emit("help_started", question=question, note=note, mode=mode)
        self._help_local.gen = gen
        try:
            answer = self.assistant.answer(
                self.context, transcript, question, note=note,
                on_delta=lambda text: self._emit_for_draft(gen, "help_delta", text=text),
                mode=mode)
        except AssistantError as exc:
            self._emit_for_draft(gen, "error", msg=f"assistant: {exc}", help=True)
            return
        served = getattr(self.assistant, "last_served", None) or self.answer_primary
        with self._help_lock:
            if gen != self._help_gen:
                return                       # superseded while it was streaming
            with self._lock:
                self.session.add_assist(self._elapsed(), question, answer, kind=mode)
            self._emit("help", question=question, answer=answer, served=served, mode=mode)
