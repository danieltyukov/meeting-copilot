"""Headless orchestration: audio -> backend (transcribe/diarize) -> session.

The engine reads frames from an audio source and forwards them to a pluggable
transcription backend. The backend reports partial (live) and final utterances
through callbacks; the engine commits finals to the session and emits events for
the UI. Because forwarding is cheap, the capture loop never blocks — which is
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
from .audio import UtteranceSegmenter
from .backends import Backend, DeepgramBackend, FallbackSttBackend, LocalBackend
from .context import gather_context
from .diarize import Diarizer
from .net import ConnectivityMonitor
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


ANSWER_MODELS = ["haiku", "sonnet", "opus"]


class CopilotEngine:
    def __init__(self, config: EngineConfig, on_event: EventCb | None = None) -> None:
        self.cfg = config
        self.on_event = on_event or (lambda e: None)
        self.session = Session()
        self.monitor = ConnectivityMonitor(on_change=self._on_connectivity_change)
        self.assistant, self.answer_primary = self._build_assistant(config)
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
            on_switch=lambda name, reason: self._emit("answer_switch", failed=name, reason=reason))
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

    def prepare(self) -> None:
        self._emit("info", msg=f"Reading context of {self.cfg.root} ...")
        self.context = gather_context(self.cfg.root, self.cfg.context_budget)
        self.monitor.start()
        self._emit("connectivity", online=self.online)
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
        order = [None, "A", "B"]
        with self._lock:
            cur = self.session.me_label
            nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "A"
            self.session.set_me(nxt)
        self._emit("me", label=nxt)

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
            name = self.session.speaker_name(utt.speaker)
        self._emit("utterance", t=utt.t, speaker=utt.speaker, name=name, text=utt.text)

    # -- pipeline ----------------------------------------------------------
    def run(self, source) -> None:
        """Blocking capture loop. Run in a background thread."""
        try:
            with source as src:
                for frame in src.frames():
                    if self._stop.is_set():
                        break
                    if self.session.is_recording and self.backend is not None:
                        self.backend.feed(frame)
        except Exception as exc:
            self._emit("error", msg=f"audio: {exc}")
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
        self._emit("help_started", question=question, note=note, mode=mode)
        try:
            answer = self.assistant.answer(
                self.context, transcript, question, note=note,
                on_delta=lambda text: self._emit("help_delta", text=text), mode=mode)
        except AssistantError as exc:
            self._emit("error", msg=f"assistant: {exc}")
            return
        served = getattr(self.assistant, "last_served", None) or self.answer_primary
        with self._lock:
            self.session.add_assist(self._elapsed(), question, answer, kind=mode)
        self._emit("help", question=question, answer=answer, served=served, mode=mode)
