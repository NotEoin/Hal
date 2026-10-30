"""Tests for gateway-driven limits: `GET /v1/models`, profile keys on the wire, overflow retry."""

from halagent.loop import Agent, effective_settings
from halagent.profiles import resolve_profile
from halagent.provider import Completion, ModelInfo, ProviderError
from halagent.settings import AgentSettings
from halagent.tools import Toolset
from halkit.testing.model_server import MODEL_ID, Reply


class StubProvider:
    """Records what the agent sends; replies come from `script`, exceptions are raised."""

    def __init__(self, info=None, script=()):
        self.info, self.script, self.sent, self.extra = info or ModelInfo(), list(script), [], {}

    def model_info(self):
        return self.info

    def complete(self, messages, tools=None):
        self.sent.append([dict(m) for m in messages])
        item = self.script.pop(0) if self.script else Completion(text="ok")
        if isinstance(item, Exception):
            raise item
        return item


def test_model_info_reads_context_length_from_the_model_list(server):
    from halagent.provider import ChatProvider

    server.context_length = 16384
    info = ChatProvider(server.url, MODEL_ID).model_info()
    assert info == ModelInfo(context_length=16384, hal_profile="")


def test_model_info_is_empty_when_the_model_is_not_listed(server):
    from halagent.provider import ChatProvider

    assert ChatProvider(server.url, "other").model_info() == ModelInfo()


def test_gateway_window_replaces_the_default_but_not_an_explicit_setting():
    prof = resolve_profile("fake")
    default = effective_settings(AgentSettings(endpoint="x"), prof, ModelInfo(context_length=16384))
    explicit = effective_settings(
        AgentSettings(endpoint="x", context_window=20000), prof, ModelInfo(context_length=16384)
    )
    assert (default.context_window, explicit.context_window) == (16384, 20000)
    assert default.max_output <= 16384 // 4


def test_profile_from_hal_profile_family_is_used(tmp_path):
    stub = StubProvider(ModelInfo(hal_profile="gemma4"))
    agent = Agent(AgentSettings(endpoint="x", model="unknown-alias"), Toolset(tmp_path), provider=stub)
    assert agent.profile.family == "gemma4"


def test_server_context_overflow_shrinks_the_budget_and_retries_once(tmp_path):
    err = ProviderError("HTTP 400: context_length_exceeded", status=400)
    stub = StubProvider(script=[err, Completion(text="done")])
    agent = Agent(AgentSettings(endpoint="x"), Toolset(tmp_path), provider=stub)
    before = agent.context.input_budget
    result = agent.run("hi")
    assert (result.stopped, len(stub.sent), agent.context.input_budget < before) == ("done", 2, True)


def test_second_context_overflow_ends_the_turn(tmp_path):
    err = ProviderError("HTTP 400: context_length_exceeded", status=400)
    stub = StubProvider(script=[err, err])
    result = Agent(AgentSettings(endpoint="x"), Toolset(tmp_path), provider=stub).run("hi")
    assert result.stopped == "context_overflow"


def test_other_provider_errors_still_raise(tmp_path):
    stub = StubProvider(script=[ProviderError("HTTP 400: bad", status=400)])
    agent = Agent(AgentSettings(endpoint="x"), Toolset(tmp_path), provider=stub)
    try:
        agent.run("hi")
    except ProviderError:
        return
    raise AssertionError("expected ProviderError")


def test_system_role_and_alternation_from_the_profile_shape_the_request(tmp_path):
    stub = StubProvider()
    profile = resolve_profile("x", overrides={"system_role": "user", "strict_alternation": True})
    agent = Agent(AgentSettings(endpoint="x"), Toolset(tmp_path), provider=stub, profile=profile)
    agent.run("hi")
    roles = [m["role"] for m in stub.sent[0]]
    assert roles == ["user"]
    assert stub.sent[0][0]["content"].endswith("hi")


def test_fake_server_end_to_end_uses_its_window(server, tmp_path):
    server.context_length = 12000
    server.script(Reply(text="fine"))
    agent = Agent(AgentSettings(endpoint=server.url, model=MODEL_ID), Toolset(tmp_path))
    assert agent.settings.context_window == 12000
