import pytest

from meeting_copilot.assistant import (DEFAULT_OLLAMA_MODEL, ApiAssistant, Assistant,
                                         AssistantError, ChainAssistant, CliAssistant,
                                         OllamaAssistant)


def test_build_user_prompt_contains_parts():
    a = Assistant()
    p = a.build_user_prompt("CTX-XYZ", "Q: hi\nA: hello", "What is your favourite tool?")
    assert "CTX-XYZ" in p
    assert "What is your favourite tool?" in p
    assert "CONVERSATION SO FAR" in p


def test_empty_sections_have_placeholders():
    a = Assistant()
    p = a.build_user_prompt("", "", "Why?")
    assert "(no context gathered)" in p
    assert "(nothing yet)" in p


def test_note_included_only_when_present():
    a = Assistant()
    with_note = a.build_user_prompt("c", "t", "q", note="focus on the scaling story")
    assert "MY EXTRA INSTRUCTION: focus on the scaling story" in with_note
    without = a.build_user_prompt("c", "t", "q")
    assert "MY EXTRA INSTRUCTION" not in without


def test_answer_blocking_path(monkeypatch):
    a = Assistant()
    captured = {}
    monkeypatch.setattr(a, "is_available", lambda: True)

    def fake_blocking(user, mode="answer"):
        captured["u"] = user
        return "ANSWER"

    monkeypatch.setattr(a, "_run_blocking", fake_blocking)
    out = a.answer("ctx", "tr", "tell me about it", note="be brief")
    assert out == "ANSWER"
    assert "tell me about it" in captured["u"]
    assert "be brief" in captured["u"]


def test_answer_streaming_path(monkeypatch):
    a = Assistant()
    monkeypatch.setattr(a, "is_available", lambda: True)

    def fake_stream(user, on_delta, mode="answer"):
        on_delta("Hello")
        on_delta("Hello world")
        return "Hello world"

    monkeypatch.setattr(a, "_run_streaming", fake_stream)
    seen = []
    out = a.answer("c", "t", "q", on_delta=lambda x: seen.append(x))
    assert out == "Hello world"
    assert seen == ["Hello", "Hello world"]  # partials streamed in order


def test_model_and_effort_are_switchable():
    a = Assistant(model="sonnet", effort="low")
    a.set_model("haiku")
    a.set_effort("high")
    assert a.model == "haiku"
    assert a.effort == "high"
    cmd = a._cmd("hi", stream=False)
    assert "haiku" in cmd and "high" in cmd
    assert "--strict-mcp-config" in cmd  # startup stripped for speed


def test_missing_binary_raises():
    a = Assistant(binary="definitely-not-a-real-binary-xyz")
    assert not a.is_available()
    with pytest.raises(AssistantError):
        a.answer("c", "t", "q")


# -- chain (API -> CLI -> local) + offline routing -------------------------
class _FakeBackend:
    def __init__(self, name, available=True, raise_err=False):
        self.name = name
        self.available = available
        self.raise_err = raise_err
        self.model = "sonnet"
        self.calls = 0

    def is_available(self):
        return self.available

    def set_model(self, m):
        self.model = m

    def answer(self, c, t, q, note="", on_delta=None, mode="answer"):
        self.calls += 1
        if self.raise_err:
            raise AssistantError(f"{self.name} boom")
        if on_delta:
            on_delta(self.name)
        return self.name.upper()


def _chain(api, cli, local, online=True, on_switch=None):
    return ChainAssistant(
        [("api", api, True), ("cli", cli, True), ("local", local, False)],
        is_online=lambda: online, on_switch=on_switch)


def test_chain_uses_api_when_online_and_ok():
    api, cli, local = _FakeBackend("api"), _FakeBackend("cli"), _FakeBackend("local")
    ch = _chain(api, cli, local, online=True)
    assert ch.answer("c", "t", "q") == "API"
    assert ch.last_served == "api"
    assert api.calls == 1 and cli.calls == 0 and local.calls == 0


def test_chain_falls_through_to_cli_on_error():
    seen = []
    api = _FakeBackend("api", raise_err=True)
    cli, local = _FakeBackend("cli"), _FakeBackend("local")
    ch = _chain(api, cli, local, online=True, on_switch=lambda n, r: seen.append(n))
    assert ch.answer("c", "t", "q") == "CLI"
    assert ch.last_served == "cli"
    assert seen == ["api"]                       # api failed, moved on


def test_chain_offline_skips_network_uses_local():
    api, cli, local = _FakeBackend("api"), _FakeBackend("cli"), _FakeBackend("local")
    ch = _chain(api, cli, local, online=False)   # OFFLINE
    assert ch.answer("c", "t", "q") == "LOCAL"
    assert ch.last_served == "local"
    assert api.calls == 0 and cli.calls == 0      # network backends skipped entirely


def test_chain_offline_without_local_raises():
    api, cli = _FakeBackend("api"), _FakeBackend("cli")
    ch = ChainAssistant([("api", api, True), ("cli", cli, True)], is_online=lambda: False)
    with pytest.raises(AssistantError) as exc:
        ch.answer("c", "t", "q")
    assert "offline" in str(exc.value).lower()


def test_chain_set_model_propagates():
    api, cli, local = _FakeBackend("api"), _FakeBackend("cli"), _FakeBackend("local")
    _chain(api, cli, local).set_model("haiku")
    assert api.model == "haiku" and cli.model == "haiku"


def test_ollama_set_model_is_noop():
    o = OllamaAssistant(model=DEFAULT_OLLAMA_MODEL)
    o.set_model("haiku")          # 1/2/3 picks Claude models; local keeps its own
    assert o.model == DEFAULT_OLLAMA_MODEL


def test_ollama_parses_streamed_chat(monkeypatch):
    import io, json as _json
    o = OllamaAssistant(model=DEFAULT_OLLAMA_MODEL)
    lines = [
        _json.dumps({"message": {"content": "Hello"}, "done": False}),
        _json.dumps({"message": {"content": " world"}, "done": False}),
        _json.dumps({"message": {"content": ""}, "done": True}),
    ]
    class _Resp(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    monkeypatch.setattr("urllib.request.urlopen",
                        lambda req, timeout=None: _Resp(("\n".join(lines)).encode()))
    seen = []
    out = o.answer("c", "t", "q", on_delta=seen.append)
    assert out == "Hello world"
    assert seen[-1] == "Hello world"


def test_api_model_id_mapping():
    a = ApiAssistant(api_key="x", model="haiku")
    assert a._model_id() == "claude-haiku-4-5"
    a.set_model("opus")
    assert a._model_id() == "claude-opus-4-8"
    a.set_model("claude-some-future-id")  # pass-through for unknown names
    assert a._model_id() == "claude-some-future-id"


def test_api_availability_from_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert ApiAssistant(api_key="sk-test").is_available()
    assert not ApiAssistant(api_key=None).is_available()
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-env")
    assert ApiAssistant(api_key=None).is_available()  # picks up env key


# -- talking points mode ------------------------------------------------------
def test_points_mode_has_its_own_system_rules():
    from meeting_copilot.assistant import SYSTEM_RULES, system_rules
    assert system_rules("answer") == SYSTEM_RULES
    points = system_rules("points")
    assert points != SYSTEM_RULES
    assert "TALKING POINTS" in points.upper()
    assert "FIRST PERSON" in points.upper()


def test_points_prompt_anchors_on_the_last_line_not_a_question():
    from meeting_copilot.assistant import build_user_prompt
    p = build_user_prompt("CTX", "Speaker A: We shipped v2 last week.", "We shipped v2 last week.",
                          mode="points")
    assert "continue from here" in p.lower()
    assert "LATEST QUESTION" not in p
    assert p.rstrip().endswith("talking points:")


def test_points_prompt_with_empty_transcript_asks_for_openers():
    from meeting_copilot.assistant import build_user_prompt
    p = build_user_prompt("CTX", "", "", mode="points")
    assert "not started" in p.lower()


def test_answer_mode_is_the_default_and_unchanged():
    from meeting_copilot.assistant import build_user_prompt
    assert build_user_prompt("c", "t", "q") == build_user_prompt("c", "t", "q", mode="answer")
    assert "LATEST QUESTION" in build_user_prompt("c", "t", "q")


def test_cli_answer_threads_mode_into_prompt_and_system(monkeypatch):
    a = Assistant()
    monkeypatch.setattr(a, "is_available", lambda: True)
    seen = {}

    def fake_blocking(user, mode="answer"):
        seen["user"] = user
        return "- point one"

    monkeypatch.setattr(a, "_run_blocking", fake_blocking)
    a.answer("ctx", "Speaker A: hi", "hi", mode="points")
    assert "continue from here" in seen["user"].lower()
    cmd = a._cmd("x", stream=False, mode="points")
    sys_arg = cmd[cmd.index("--system-prompt") + 1]
    assert "TALKING POINTS" in sys_arg.upper()


def test_chain_passes_mode_through():
    class _ModeBackend(_FakeBackend):
        def answer(self, c, t, q, note="", on_delta=None, mode="answer"):
            self.mode = mode
            return mode

    b = _ModeBackend("api")
    ch = ChainAssistant([("api", b, True)], is_online=lambda: True)
    assert ch.answer("c", "t", "q", mode="points") == "points"
    assert b.mode == "points"
