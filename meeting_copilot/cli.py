"""Command-line entry point: ``meeting-copilot``.

Run it inside any project directory. Default launches the live TUI on your
microphone, transcribing via Deepgram streaming when a key is configured (or
local Whisper otherwise). Flags drive the same engine headlessly and self-check
the install.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from . import __version__
from .assistant import DEFAULT_OLLAMA_MODEL
from .engine import CopilotEngine, EngineConfig
from .exe import find_executable

CONFIG_ENV = Path.home() / ".config" / "meeting-copilot" / "config.env"


def load_config_env() -> None:
    """Merge ~/.config/meeting-copilot/config.env into the environment.

    Existing environment variables win, so an explicit export overrides the file.
    """
    if not CONFIG_ENV.is_file():
        return
    try:
        # utf-8-sig: Windows editors may save a BOM, which would hide the first key.
        for line in CONFIG_ENV.read_text(encoding="utf-8-sig").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            os.environ.setdefault(key.strip(), val.strip())
    except OSError:
        pass


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="meeting-copilot",
        description="Sparky, a live meeting copilot: transcribe the room and draft "
                    "first-person answers from your project's context.",
    )
    p.add_argument("dir", nargs="?", default=".",
                   help="Project directory whose context to use (default: current dir).")
    p.add_argument("--stt", choices=["auto", "deepgram", "local"], default="auto",
                   help="Transcription backend (default: auto, Deepgram if a key is set, else local).")
    p.add_argument("--deepgram-model", default="nova-3",
                   help="Deepgram model (default: nova-3).")
    p.add_argument("--no-stt-fallback", action="store_true",
                   help="Disable automatic Deepgram -> local Whisper fallback.")
    p.add_argument("--model", default="base", choices=["tiny", "base", "small", "medium"],
                   help="Local Whisper model size when --stt local (default: base).")
    p.add_argument("--answer-model", default="sonnet",
                   help="Claude model for drafting answers (default: sonnet; switch live with 1/2/3).")
    p.add_argument("--effort", default="low", choices=["low", "medium", "high", "xhigh"],
                   help="Reasoning effort for CLI answers (default: low, the fastest).")
    p.add_argument("--answer-backend", choices=["auto", "api", "cli"], default="auto",
                   help="Answer generation: auto (API if ANTHROPIC_API_KEY set, else CLI), "
                        "with automatic CLI fallback whenever the API fails.")
    p.add_argument("--ollama-model", default=DEFAULT_OLLAMA_MODEL,
                   help="Local LLM (via Ollama) for offline answers. Swap in any "
                        f"Ollama tag here to switch models (default: {DEFAULT_OLLAMA_MODEL}).")
    p.add_argument("--ollama-host", default="http://localhost:11434",
                   help="Ollama server URL (default: http://localhost:11434).")
    p.add_argument("--no-diarize", action="store_true",
                   help="Disable speaker separation; transcribe everything plainly.")
    p.add_argument("--me", metavar="NAME", default=None,
                   help="Your name. Drafts know who \"I\" am, and the voice that "
                        "introduces itself with it is marked as you (default: MY_NAME "
                        "from config.env).")
    p.add_argument("--people", metavar="NAMES", default=None,
                   help="Who else is in the meeting, comma-separated: \"Sarah Chen, Marcus "
                        "Lee\". Intros match these names, the last voice left gets the "
                        "last name, and drafts and the saved transcript list them.")
    p.add_argument("--invite", metavar="FILE.ics", default=None,
                   help="Read who is in the meeting from a calendar invite (default: the "
                        "one .ics file in the project directory, if there is exactly one).")
    p.add_argument("--language", default="en",
                   help="Transcription language code (default: en).")
    p.add_argument("--context-budget", type=int, default=60000,
                   help="Max characters of project context sent to the model "
                        "(default: 60000). Raise it for a rich, hand-written "
                        "context (e.g. a presentation/slide deck), lower it to cut cost.")
    p.add_argument("--mic", metavar="DEVICE", default=None,
                   help="Microphone to listen on, by the name --list-mics shows "
                        "(default: the system default; on Windows the first one found).")
    p.add_argument("--list-mics", action="store_true",
                   help="List the microphones ffmpeg can open here, then exit.")
    p.add_argument("--audio-file", default=None,
                   help="Use an audio file instead of the microphone (demo/testing).")
    p.add_argument("--headless", action="store_true",
                   help="No UI: auto-start, run the source to completion, print + export. "
                        "Requires --audio-file.")
    p.add_argument("--self-test", action="store_true",
                   help="Check ffmpeg, Claude CLI, the STT backend and context, then exit.")
    p.add_argument("--print-context", action="store_true",
                   help="Print the gathered directory context and exit.")
    p.add_argument("--version", action="version", version=f"meeting-copilot {__version__}")
    return p


def _resolve_backend(args) -> tuple[str, str | None]:
    key = os.environ.get("DEEPGRAM_API_KEY")
    backend = args.stt
    if backend == "auto":
        backend = "deepgram" if key else "local"
    return backend, key


def _my_name(args) -> str | None:
    """--me wins over MY_NAME (from the environment or config.env)."""
    return (args.me or os.environ.get("MY_NAME") or "").strip() or None


def _make_config(args) -> EngineConfig:
    backend, key = _resolve_backend(args)
    return EngineConfig(
        root=Path(args.dir).expanduser().resolve(),
        stt_backend=backend,
        stt_fallback=not args.no_stt_fallback,
        deepgram_api_key=key,
        deepgram_model=args.deepgram_model,
        whisper_model=args.model,
        answer_model=args.answer_model,
        answer_effort=args.effort,
        answer_backend=args.answer_backend,
        anthropic_api_key=os.environ.get("ANTHROPIC_API_KEY"),
        ollama_model=args.ollama_model,
        ollama_host=args.ollama_host,
        diarize=not args.no_diarize,
        language=args.language,
        context_budget=args.context_budget,
        my_name=_my_name(args),
        people=args.people,
        invite=Path(args.invite).expanduser() if args.invite else None,
    )


FFMPEG_HINTS = {
    "darwin": "brew install ffmpeg",
    "win32": "winget install Gyan.FFmpeg",
}


def _ffmpeg_hint() -> str:
    return FFMPEG_HINTS.get(sys.platform, "sudo apt install ffmpeg (or your distro's package)")


def _list_mics() -> int:
    from .audio import AudioError, list_mics

    try:
        mics = list_mics()
    except AudioError as exc:
        hint = f" Install it: {_ffmpeg_hint()}" if find_executable("ffmpeg") is None else ""
        print(f"Could not list microphones: {exc}.{hint}", file=sys.stderr)
        return 1
    if not mics:
        print("ffmpeg found no microphones.")
        return 1
    for mic in mics:
        label = f"  ({mic.label})" if mic.label else ""
        print(f"{'*' if mic.default else ' '} {mic.name}{label}")
    # macOS does not say which input is its default, so nothing is starred there.
    used = "* is used by default." if any(m.default for m in mics) else \
        "The system's default input is used."
    print(f"\n{used} Pick another with: meeting-copilot --mic \"NAME\"")
    return 0


def _self_test(args) -> int:
    from .assistant import ApiAssistant, AssistantError, CliAssistant, OllamaAssistant
    from .context import gather_context
    from .net import check_online

    ok = True
    backend, key = _resolve_backend(args)
    api_key = os.environ.get("ANTHROPIC_API_KEY")
    answer_mode = "api→cli" if (args.answer_backend in ("auto", "api") and api_key) else "cli"
    print("Sparky self-test\n" + "-" * 30)
    print(f"STT backend: {backend}    Answer backend: {answer_mode}→local")

    online = check_online()
    print(f"[INFO] connectivity: {'● online' if online else '● OFFLINE'}")

    have_ffmpeg = find_executable("ffmpeg") is not None
    print(f"[{'PASS' if have_ffmpeg else 'FAIL'}] ffmpeg on PATH"
          + ("" if have_ffmpeg else f" (install it: {_ffmpeg_hint()})"))
    ok &= have_ffmpeg
    if have_ffmpeg:
        from .audio import AudioError, list_mics
        try:
            mics = list_mics()
        except AudioError as exc:
            mics, why = [], str(exc)
        else:
            why = "ffmpeg found none"
        chosen = args.mic or next((m.name for m in mics if m.default), None)
        if chosen:
            print(f"[PASS] microphone: {chosen}")
        elif sys.platform == "win32":
            print(f"[FAIL] no microphone: {why}")       # nothing to fall back to
            ok = False
        else:
            print("[INFO] microphone: the system default")

    # Offline path: local Whisper (transcription) + local LLM (answers).
    ollama = OllamaAssistant(model=args.ollama_model, host=args.ollama_host)
    if ollama.is_available():
        print(f"[PASS] offline answers ready: Ollama at {args.ollama_host} ({args.ollama_model})")
    else:
        print(f"[INFO] offline answers OFF: no Ollama at {args.ollama_host}. "
              f"Install Ollama + `ollama pull {args.ollama_model}` for no-WiFi answers.")

    # Answer path: API (if configured) is primary; the CLI is the required fallback.
    if answer_mode == "api→cli":
        try:
            reply = ApiAssistant(api_key=api_key, model=args.answer_model).ping()
            good = "PONG" in reply.upper()
            print(f"[{'PASS' if good else 'WARN'}] Anthropic API round-trip: {reply!r}")
        except Exception as exc:
            print(f"[WARN] Anthropic API round-trip failed (will fall back to CLI): "
                  f"{str(exc)[:160]}")

    cli = CliAssistant(model=args.answer_model)
    have_claude = cli.is_available()
    print(f"[{'PASS' if have_claude else 'FAIL'}] claude CLI on PATH (fallback)")
    ok &= have_claude
    if have_claude:
        try:
            reply = cli.ping()
            good = "PONG" in reply.upper()
            print(f"[{'PASS' if good else 'WARN'}] claude CLI round-trip: {reply!r}")
            ok &= good
        except AssistantError as exc:
            print(f"[FAIL] claude CLI round-trip: {exc}")
            ok = False

    if backend == "deepgram":
        if not key:
            print("[FAIL] DEEPGRAM_API_KEY not set")
            ok = False
        else:
            try:
                from .backends import DeepgramBackend
                b = DeepgramBackend(api_key=key, model=args.deepgram_model)
                b.start(lambda *a: None, lambda *a: None)
                b.finish()
                print("[PASS] Deepgram stream connected")
            except Exception as exc:
                print(f"[FAIL] Deepgram connect: {exc}")
                ok = False
    else:
        try:
            from .transcribe import Transcriber
            print(f"[..] loading Whisper '{args.model}'…")
            Transcriber(model_size=args.model).load()
            print(f"[PASS] Whisper '{args.model}' loaded")
        except Exception as exc:
            print(f"[FAIL] Whisper load: {exc}")
            ok = False

    ctx = gather_context(Path(args.dir).resolve())
    print(f"[{'PASS' if ctx else 'FAIL'}] context gathered ({len(ctx)} chars)")
    ok &= bool(ctx)

    print("-" * 30)
    print("RESULT:", "ALL GOOD" if ok else "PROBLEMS FOUND")
    return 0 if ok else 1


def _run_headless(args) -> int:
    from .audio import FileSource

    if not args.audio_file:
        print("--headless requires --audio-file", file=sys.stderr)
        return 2

    def on_event(e: dict) -> None:
        t = e["type"]
        if t == "utterance":
            print(f"[{e['t']:6.1f}s] {e['name']}: {e['text']}")
        elif t == "names" and e.get("source") == "intro":
            print(f"… Speaker {e['label']} is {e['name']}")
        elif t == "names" and e.get("source") == "roster":
            print(f"… Speaker {e['label']} is {e['name']}, the one name left")
        elif t == "me" and e.get("by") == "name":
            print(f"… Speaker {e['label']} said your name, so that voice is you")
        elif t == "info":
            print(f"… {e['msg']}")
        elif t == "exported":
            print(f"\nSaved transcript → {e['path']} ({e['utterances']} utterances)")
        elif t == "error":
            print(f"ERROR: {e['msg']}", file=sys.stderr)

    engine = CopilotEngine(_make_config(args), on_event=on_event)
    # Deepgram streaming expects ~real-time audio, so pace the file; local STT
    # can consume as fast as it likes.
    realtime = engine.cfg.stt_backend == "deepgram"
    engine.prepare()
    engine.start_meeting()
    engine.run(FileSource(args.audio_file, realtime=realtime))  # blocks until the file ends
    engine.end_meeting()  # finish() drains pending streaming finals
    return 0


def _run_tui(args) -> int:
    from .app import CopilotTUI
    from .audio import FileSource, MicSource

    if args.audio_file:
        def factory():
            return FileSource(args.audio_file, realtime=True)
    else:
        def factory():
            return MicSource(args.mic)

    engine = CopilotEngine(_make_config(args))
    if not engine.assistant.is_available():
        print("Warning: 'claude' CLI not found. Answers and talking points will not work.",
              file=sys.stderr)
    CopilotTUI(engine, factory).run()
    return 0


def _utf8_output() -> None:
    """On Windows, print UTF-8 even when piped: the locale code page there
    cannot encode names in most scripts, nor the self-test's online marker."""
    if sys.platform != "win32":
        return
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, ValueError):
            pass


def main(argv: list[str] | None = None) -> int:
    _utf8_output()
    load_config_env()
    args = _build_parser().parse_args(argv)

    if args.list_mics:
        return _list_mics()

    if args.print_context:
        from .context import gather_context
        print(gather_context(Path(args.dir).resolve()))
        return 0
    if args.self_test:
        return _self_test(args)
    if args.headless:
        return _run_headless(args)
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        print("The live UI needs an interactive terminal. "
              "Use --headless --audio-file <path> for non-interactive runs.",
              file=sys.stderr)
        return 2
    return _run_tui(args)


if __name__ == "__main__":
    raise SystemExit(main())
