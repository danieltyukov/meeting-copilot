"""Draft a spoken answer, with an API-first / CLI-fallback strategy.

Three pieces share one interface (``answer(ctx, transcript, q, note, on_delta, mode)``),
and every backend drafts in one of two modes:

* ``answer`` (default) — a first-person reply to the latest question put to me.
* ``points`` — first-person talking points to carry the conversation forward
  from wherever it is: build on the last thing said, bring in something from my
  context the other side has not heard yet, ask something back.

* ``ApiAssistant`` — the Anthropic API via the official SDK, streamed. Fast and
  consistent (no subprocess cold-start; separate quota). Configured for speed:
  haiku/sonnet, thinking off.
* ``CliAssistant`` — the authenticated ``claude`` CLI in single-shot mode. No API
  key needed; works with whatever auth Claude Code already has.
* ``FallbackAssistant`` — wraps a primary and a secondary. Tries the primary; on
  *any* failure (missing key, billing, network, empty result) it signals the UI
  and re-streams from the secondary — even mid-meeting.

This keeps the room covered: the API gives speed, the CLI guarantees an answer.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import threading
import urllib.error
import urllib.request
from typing import Callable

SYSTEM_RULES = """You are my real-time meeting copilot. I am in a live conversation in person, \
about the project described in the user's message. Read the project context and the live \
transcript, then draft MY reply to the latest question put to me.

Rules:
- Write in the FIRST PERSON, as the words I should say out loud. No preamble, no \
meta-commentary, no "you could say" — just my answer.
- Keep it concise and natural: something I can speak in roughly 20-40 seconds.
- Be concrete and specific to THIS project; cite real details from the context when relevant.
- For behavioral or open-ended questions, answer confidently and honestly the way an engineer \
who built this project would.
- Several people may be in the room; "Speaker A", "Speaker B" and so on are different voices.
- If the message contains a line starting with "MY EXTRA INSTRUCTION:", treat it as the most \
important steer for what I want from the answer, and follow it closely.
- Do not use any tools. Answer directly from what is provided."""

POINTS_RULES = """You are my real-time meeting copilot. I am in a live conversation in person, \
about the project described in the user's message. Read the project context and the live \
transcript, then give me TALKING POINTS I can use to carry the conversation forward from \
exactly where it is now.

Rules:
- 3 to 5 points. Each is ONE line I can say out loud as-is, in the FIRST PERSON, starting \
with "- ". Nothing else: no preamble, no headings, no meta-commentary.
- Pick up from the last thing said. Build on it, add something concrete from my context the \
other side has not heard yet, or steer toward what I want to cover.
- Make at least one point a question I can ask them, so the conversation keeps moving.
- Be specific to THIS project; cite real details from the context. No generic filler.
- If the conversation has not started yet, give me points to open with.
- Several people may be in the room; "Speaker A", "Speaker B" and so on are different voices.
- If the message contains a line starting with "MY EXTRA INSTRUCTION:", treat it as the most \
important steer for what I want, and follow it closely.
- Do not use any tools. Work directly from what is provided."""

MODES = ("answer", "points")


def system_rules(mode: str = "answer") -> str:
    """The system prompt for a drafting mode (``answer`` or ``points``)."""
    return POINTS_RULES if mode == "points" else SYSTEM_RULES

# Friendly names the UI cycles through -> concrete API model IDs.
API_MODEL_IDS = {
    "haiku": "claude-haiku-4-5",
    "sonnet": "claude-sonnet-4-6",
    "opus": "claude-opus-4-8",
}

# Default local LLM for the offline answer path (served by Ollama). One source of
# truth so the model can be swapped in a single place. Qwen3 4B Instruct (2507) is
# the pick over a larger or "thinking" model: it's a dedicated *non-thinking* text
# instruct model, so on a CPU-only box it streams an answer immediately instead of
# spending seconds on hidden reasoning — the speed-first behaviour this tool needs.
# Override at runtime with `--ollama-model` (any Ollama tag works).
DEFAULT_OLLAMA_MODEL = "qwen3:4b-instruct-2507-q4_K_M"


class AssistantError(RuntimeError):
    pass


def build_user_prompt(context: str, transcript: str, question: str, note: str = "",
                      mode: str = "answer") -> str:
    """Assemble the user message. ``question`` is the latest question in ``answer``
    mode and the last thing anyone said in ``points`` mode (empty before the
    conversation starts)."""
    parts = [
        f"=== PROJECT CONTEXT ===\n{context.strip() or '(no context gathered)'}",
        f"=== CONVERSATION SO FAR ===\n{transcript.strip() or '(nothing yet)'}",
    ]
    if mode == "points":
        anchor = question.strip() or "(the conversation has not started yet: give me points to open with)"
        parts.append(f"=== LAST THING SAID (continue from here) ===\n{anchor}")
    else:
        parts.append(f"=== LATEST QUESTION (answer this) ===\n{question.strip()}")
    if note.strip():
        parts.append(f"MY EXTRA INSTRUCTION: {note.strip()}")
    parts.append("Now write my talking points:" if mode == "points" else "Now write my spoken answer:")
    return "\n\n".join(parts)


# --------------------------------------------------------------------------- #
# CLI backend
# --------------------------------------------------------------------------- #
class CliAssistant:
    def __init__(self, model: str | None = "sonnet", effort: str = "low",
                 timeout: int = 90, binary: str = "claude") -> None:
        self.model = model
        self.effort = effort
        self.timeout = timeout
        self.binary = binary

    def is_available(self) -> bool:
        return shutil.which(self.binary) is not None

    def set_model(self, model: str) -> None:
        self.model = model

    def set_effort(self, effort: str) -> None:
        self.effort = effort

    def build_user_prompt(self, context: str, transcript: str, question: str, note: str = "",
                          mode: str = "answer") -> str:
        return build_user_prompt(context, transcript, question, note, mode)

    def _cmd(self, user_prompt: str, stream: bool, mode: str = "answer") -> list[str]:
        cmd = [
            self.binary, "-p", user_prompt,
            "--system-prompt", system_rules(mode),
            "--strict-mcp-config",          # skip loading configured MCP servers
            "--setting-sources", "",        # skip skills/plugins/hooks
            "--effort", self.effort,
        ]
        if self.model:
            cmd += ["--model", self.model]
        if stream:
            cmd += ["--output-format", "stream-json", "--include-partial-messages", "--verbose"]
        else:
            cmd += ["--output-format", "text"]
        return cmd

    def _env(self) -> dict:
        env = dict(os.environ)
        env.pop("CLAUDE_EFFORT", None)  # our --effort flag should decide
        # The CLI must use Claude Code's own (subscription) auth. An ANTHROPIC_API_KEY
        # in the environment is meant for the API path; if leaked here the CLI tries
        # to auth with it and fails — defeating the whole point of the fallback.
        env.pop("ANTHROPIC_API_KEY", None)
        env.pop("ANTHROPIC_AUTH_TOKEN", None)
        return env

    def answer(self, context: str, transcript: str, question: str, note: str = "",
               on_delta: Callable[[str], None] | None = None, mode: str = "answer") -> str:
        if not self.is_available():
            raise AssistantError(
                f"'{self.binary}' CLI not found on PATH. Install/login to Claude Code first.")
        user = build_user_prompt(context, transcript, question, note, mode)
        if on_delta is None:
            return self._run_blocking(user, mode=mode)
        return self._run_streaming(user, on_delta, mode=mode)

    def _run_blocking(self, user: str, mode: str = "answer") -> str:
        try:
            proc = subprocess.run(
                self._cmd(user, stream=False, mode=mode), cwd=tempfile.gettempdir(),
                capture_output=True, text=True, timeout=self.timeout, env=self._env())
        except subprocess.TimeoutExpired as exc:
            raise AssistantError(f"Claude timed out after {self.timeout}s") from exc
        if proc.returncode != 0:
            raise AssistantError(f"Claude exited {proc.returncode}: {proc.stderr.strip()[:500]}")
        answer = proc.stdout.strip()
        if not answer:
            raise AssistantError("Claude returned an empty answer")
        return answer

    def _run_streaming(self, user: str, on_delta: Callable[[str], None],
                       mode: str = "answer") -> str:
        try:
            proc = subprocess.Popen(
                self._cmd(user, stream=True, mode=mode), cwd=tempfile.gettempdir(),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                bufsize=1, env=self._env())
        except FileNotFoundError as exc:
            raise AssistantError(f"Could not run '{self.binary}'") from exc

        killer = threading.Timer(self.timeout, proc.kill)
        killer.start()
        parts: list[str] = []
        result: str | None = None
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except ValueError:
                    continue
                if data.get("type") == "stream_event":
                    ev = data.get("event", {})
                    if (ev.get("type") == "content_block_delta"
                            and ev.get("delta", {}).get("type") == "text_delta"):
                        parts.append(ev["delta"]["text"])
                        on_delta("".join(parts))
                elif data.get("type") == "result":
                    result = data.get("result") or result
        finally:
            killer.cancel()
            proc.wait()
        answer = (result or "".join(parts)).strip()
        if not answer:
            err = (proc.stderr.read() if proc.stderr else "").strip()
            raise AssistantError(f"Claude returned no answer. {err[:300]}")
        return answer

    def ping(self) -> str:
        return self._run_blocking("Reply with exactly the single word: PONG")


# Backwards-compatible alias (the CLI path was the original Assistant).
Assistant = CliAssistant


# --------------------------------------------------------------------------- #
# API backend (Anthropic SDK, streamed)
# --------------------------------------------------------------------------- #
class ApiAssistant:
    def __init__(self, api_key: str | None = None, model: str = "sonnet",
                 max_tokens: int = 512, timeout: int = 60) -> None:
        self.api_key = api_key
        self.model = model
        self.max_tokens = max_tokens
        self.timeout = timeout
        self._client = None

    def is_available(self) -> bool:
        return bool(self.api_key or os.environ.get("ANTHROPIC_API_KEY"))

    def set_model(self, model: str) -> None:
        self.model = model

    def set_effort(self, effort: str) -> None:  # accepted for interface parity; API runs fast
        pass

    def _model_id(self) -> str:
        return API_MODEL_IDS.get(self.model, self.model)

    def _ensure_client(self):
        if self._client is None:
            try:
                import anthropic
            except ImportError as exc:
                raise AssistantError("anthropic SDK not installed") from exc
            kwargs = {"timeout": self.timeout}
            if self.api_key:
                kwargs["api_key"] = self.api_key
            self._client = anthropic.Anthropic(**kwargs)
        return self._client

    def answer(self, context: str, transcript: str, question: str, note: str = "",
               on_delta: Callable[[str], None] | None = None, mode: str = "answer") -> str:
        client = self._ensure_client()
        user = build_user_prompt(context, transcript, question, note, mode)
        parts: list[str] = []
        try:
            # No `thinking` param => thinking off on haiku/sonnet/opus: fastest path.
            with client.messages.stream(
                model=self._model_id(),
                max_tokens=self.max_tokens,
                system=system_rules(mode),
                messages=[{"role": "user", "content": user}],
            ) as stream:
                for text in stream.text_stream:
                    parts.append(text)
                    if on_delta:
                        on_delta("".join(parts))
        except AssistantError:
            raise
        except Exception as exc:  # SDK/network/auth errors -> trigger fallback
            raise AssistantError(f"API error: {type(exc).__name__}: {str(exc)[:300]}") from exc
        answer = "".join(parts).strip()
        if not answer:
            raise AssistantError("API returned an empty answer")
        return answer

    def ping(self) -> str:
        client = self._ensure_client()
        msg = client.messages.create(
            model=self._model_id(), max_tokens=16,
            messages=[{"role": "user", "content": "Reply with exactly the single word: PONG"}])
        return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text").strip()


# --------------------------------------------------------------------------- #
# Offline backend (local LLM via Ollama)
# --------------------------------------------------------------------------- #
class OllamaAssistant:
    """Answers from a local LLM via Ollama's HTTP API — works with no internet.

    Available only when an Ollama server is running locally with the model
    pulled. ``set_model`` is ignored (the 1/2/3 keys pick Claude models, which
    don't apply to a local model)."""

    needs_network = False

    def __init__(self, model: str = DEFAULT_OLLAMA_MODEL, host: str = "http://localhost:11434",
                 timeout: int = 120) -> None:
        self.model = model
        self.host = host.rstrip("/")
        self.timeout = timeout

    def set_model(self, model: str) -> None:  # keep the configured local model
        pass

    def set_effort(self, effort: str) -> None:
        pass

    def is_available(self) -> bool:
        try:
            with urllib.request.urlopen(f"{self.host}/api/tags", timeout=2) as resp:
                return resp.status == 200
        except Exception:
            return False

    def answer(self, context: str, transcript: str, question: str, note: str = "",
               on_delta: Callable[[str], None] | None = None, mode: str = "answer") -> str:
        user = build_user_prompt(context, transcript, question, note, mode)
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_rules(mode)},
                {"role": "user", "content": user},
            ],
            "stream": True,
            "options": {"num_predict": 400},
        }).encode()
        req = urllib.request.Request(f"{self.host}/api/chat", data=body,
                                     headers={"content-type": "application/json"})
        parts: list[str] = []
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                for raw in resp:
                    line = raw.decode("utf-8").strip()
                    if not line:
                        continue
                    try:
                        evt = json.loads(line)
                    except ValueError:
                        continue
                    chunk = (evt.get("message") or {}).get("content", "")
                    if chunk:
                        parts.append(chunk)
                        if on_delta:
                            on_delta("".join(parts))
                    if evt.get("error"):
                        raise AssistantError(f"ollama: {evt['error']}")
        except AssistantError:
            raise
        except Exception as exc:
            raise AssistantError(f"ollama: {type(exc).__name__}: {str(exc)[:200]}") from exc
        answer = "".join(parts).strip()
        if not answer:
            raise AssistantError("local model returned an empty answer")
        return answer

    def ping(self) -> str:
        return "PONG" if self.is_available() else ""


# --------------------------------------------------------------------------- #
# Connectivity-aware fallback chain
# --------------------------------------------------------------------------- #
class ChainAssistant:
    """Try backends in order, skipping network ones when offline.

    ``backends`` is a list of ``(name, assistant, needs_network)``. When offline,
    network backends are skipped entirely (so we don't wait on doomed timeouts)
    and the local one is used. ``on_switch(failed_name, reason)`` fires each time
    a backend fails and we move on; ``last_served`` records which one answered.
    """

    def __init__(self, backends, is_online=None, on_switch=None) -> None:
        self.backends = backends                 # [(name, assistant, needs_network)]
        self.is_online = is_online or (lambda: True)
        self.on_switch = on_switch
        self.last_served: str | None = None

    @property
    def model(self):
        for _, a, _ in self.backends:
            if getattr(a, "model", None):
                return a.model
        return None

    def is_available(self) -> bool:
        return any(a.is_available() for _, a, _ in self.backends)

    def set_model(self, model: str) -> None:
        for _, a, _ in self.backends:
            if hasattr(a, "set_model"):
                a.set_model(model)

    def set_effort(self, effort: str) -> None:
        for _, a, _ in self.backends:
            if hasattr(a, "set_effort"):
                a.set_effort(effort)

    def answer(self, context: str, transcript: str, question: str, note: str = "",
               on_delta: Callable[[str], None] | None = None, mode: str = "answer") -> str:
        online = self.is_online()
        candidates = [(n, a) for (n, a, net) in self.backends if online or not net]
        last_err: Exception | None = None
        tried_any = False
        for name, a in candidates:
            if not a.is_available():
                continue
            tried_any = True
            try:
                result = a.answer(context, transcript, question, note, on_delta, mode=mode)
                self.last_served = name
                return result
            except AssistantError as exc:
                last_err = exc
                if self.on_switch:
                    self.on_switch(name, str(exc))
        if not online and not tried_any:
            raise AssistantError(
                "Offline and no local model running — install Ollama and pull a model "
                f"(e.g. `ollama pull {DEFAULT_OLLAMA_MODEL}`) to answer with no internet.")
        raise AssistantError(str(last_err) if last_err else "no answer backend available")

    def ping(self) -> str:
        for _, a, _ in self.backends:
            try:
                if a.is_available():
                    return a.ping()
            except Exception:
                continue
        return ""
