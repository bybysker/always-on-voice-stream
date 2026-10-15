"""Claude client: request shape per model family, structured-output parsing, error mapping and the
strict-schema limits the wire schemas must stay within. No network: the SDK client is faked."""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import anthropic
import httpx
import pytest
from anthropic import transform_schema

from voicestream.brain.llm import ClaudeLLM, LLMError, LLMUnavailable, image_block, model_params, text_block
from voicestream.brain.schema import WireExtraction, WireSummary
from voicestream.costs import TokenUsage

EMPTY = {"new_items": [], "updates": [], "meeting_title": "", "participants": []}


class _Messages:
    def __init__(self, responses: list[Any]) -> None:
        self.responses = responses
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        nxt = self.responses.pop(0)
        if isinstance(nxt, BaseException):
            raise nxt
        return nxt


class _Client:
    def __init__(self, *responses: Any) -> None:
        self.messages = _Messages(list(responses))
        self.closed = False

    async def close(self) -> None:
        self.closed = True


def _resp(text: str, stop: str = "end_turn", *, thinking: bool = False) -> SimpleNamespace:
    content = [SimpleNamespace(type="thinking", thinking="let me see", signature="sig")] if thinking else []
    content.append(SimpleNamespace(type="text", text=text))
    usage = SimpleNamespace(input_tokens=50, output_tokens=20, cache_read_input_tokens=4000, cache_creation_input_tokens=300)
    return SimpleNamespace(content=content, stop_reason=stop, stop_details=None, usage=usage)


def _llm(*responses: Any) -> tuple[ClaudeLLM, _Client, list[TokenUsage]]:
    usages: list[TokenUsage] = []
    client = _Client(*responses)
    return ClaudeLLM(client=client, on_usage=usages.append), client, usages


def _status_error(cls: type[anthropic.APIStatusError], status: int) -> anthropic.APIStatusError:
    response = httpx.Response(status, request=httpx.Request("POST", "https://api.anthropic.com/v1/messages"))
    return cls("nope", response=response, body={"type": "error", "error": {"type": "x", "message": "nope"}})


async def _structured(llm: ClaudeLLM, model: str = "claude-haiku-5-5", effort: str | None = "low") -> WireExtraction:
    return await llm.structured(
        model=model, system="SYSTEM", content=[text_block("hello")], schema=WireExtraction, max_tokens=1234, effort=effort
    )


# ------------------------------------------------------------------- model params


@pytest.mark.parametrize(
    ("model", "effort", "expected"),
    [
        ("claude-haiku-5-5", "low", {"output_config": {"effort": "low"}, "thinking": {"type": "disabled"}}),
        ("claude-haiku-5-5", None, {"thinking": {"type": "disabled"}}),
        ("claude-sonnet-5-5", "medium", {"output_config": {"effort": "medium"}, "thinking": {"type": "between_tools"}}),
        ("claude-sonnet-5-5-20261001", "low", {"output_config": {"effort": "low"}, "thinking": {"type": "between_tools"}}),
        ("claude-opus-5-5", "high", {"output_config": {"effort": "high"}}),  # always thinks: no thinking key
        ("claude-fable-5-1", "low", {"output_config": {"effort": "low"}}),
        ("claude-haiku-4-5", "low", {}),
        ("claude-sonnet-4-5-20250929", "high", {}),
        ("claude-3-5-haiku-latest", "low", {}),
    ],
)
def test_model_params_per_family(model: str, effort: str | None, expected: dict[str, Any]) -> None:
    assert model_params(model, effort) == expected


def test_content_blocks() -> None:
    assert text_block("hi") == {"type": "text", "text": "hi"}
    assert text_block("hi", cache=True)["cache_control"] == {"type": "ephemeral"}
    img = image_block(b"\xff\xd8jpeg", "image/jpeg")
    assert img["type"] == "image" and img["source"]["media_type"] == "image/jpeg"
    assert base64.b64decode(img["source"]["data"]) == b"\xff\xd8jpeg"


# ------------------------------------------------------------------- structured()


async def test_structured_request_shape_and_parsing() -> None:
    llm, client, usages = _llm(_resp(json.dumps({**EMPTY, "meeting_title": "Q4 planning"}), thinking=True))
    out = await _structured(llm)
    assert isinstance(out, WireExtraction) and out.meeting_title == "Q4 planning"  # thinking block ignored
    (call,) = client.messages.calls
    assert call["model"] == "claude-haiku-5-5" and call["max_tokens"] == 1234
    assert call["system"] == [{"type": "text", "text": "SYSTEM", "cache_control": {"type": "ephemeral"}}]
    assert call["messages"] == [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
    assert call["output_config"] == {
        "effort": "low",
        "format": {"type": "json_schema", "schema": transform_schema(WireExtraction)},
    }
    assert call["thinking"] == {"type": "disabled"}
    assert not {"temperature", "top_p", "tool_choice", "tools", "betas"} & set(call)
    assert usages == [TokenUsage("claude-haiku-5-5", 50, 20, 4000, 300)]


async def test_structured_on_opus_sends_no_thinking_param() -> None:
    llm, client, _ = _llm(_resp(json.dumps(EMPTY)))
    await _structured(llm, model="claude-opus-5-5", effort="high")
    call = client.messages.calls[0]
    assert "thinking" not in call and call["output_config"]["effort"] == "high"


async def test_structured_on_older_models_sends_only_the_format() -> None:
    llm, client, _ = _llm(_resp(json.dumps(EMPTY)))
    await _structured(llm, model="claude-haiku-4-5")
    call = client.messages.calls[0]
    assert "thinking" not in call and set(call["output_config"]) == {"format"}


@pytest.mark.parametrize(
    ("response", "match"),
    [
        (_resp("", stop="refusal"), "refused"),
        (_resp('{"new_items": [', stop="max_tokens"), "max_tokens"),
        (_resp("Sure! Here are the items: none"), "invalid structured output"),
        (_resp(json.dumps({"new_items": "nope"})), "invalid structured output"),
    ],
)
async def test_structured_failures_raise_llm_error_but_are_still_billed(response: SimpleNamespace, match: str) -> None:
    llm, _, usages = _llm(response)
    with pytest.raises(LLMError, match=match) as exc:
        await _structured(llm)
    assert not isinstance(exc.value, LLMUnavailable)  # transient: the extractor retries
    assert len(usages) == 1


# ------------------------------------------------------------------- complete()


async def test_complete_returns_stripped_text() -> None:
    llm, client, usages = _llm(_resp("  Drafted.\n", thinking=True))
    text = await llm.complete(model="claude-sonnet-5-5", system="S", content=[text_block("x")], max_tokens=99, effort="medium")
    assert text == "Drafted."
    call = client.messages.calls[0]
    assert call["output_config"] == {"effort": "medium"} and call["thinking"] == {"type": "between_tools"}
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"} and call["max_tokens"] == 99
    assert usages[0].model == "claude-sonnet-5-5"


async def test_complete_refusal() -> None:
    llm, _, _ = _llm(_resp("", stop="refusal"))
    with pytest.raises(LLMError):
        await llm.complete(model="claude-haiku-5-5", system="S", content=[text_block("x")])


# ---------------------------------------------------------------------- errors


@pytest.mark.parametrize(
    "exc",
    [
        _status_error(anthropic.AuthenticationError, 401),
        _status_error(anthropic.PermissionDeniedError, 403),
        _status_error(anthropic.APIStatusError, 402),
    ],
)
async def test_credential_and_billing_errors_are_permanent(exc: Exception) -> None:
    llm, _, _ = _llm(exc)
    with pytest.raises(LLMUnavailable):
        await _structured(llm)


@pytest.mark.parametrize(
    "exc",
    [
        _status_error(anthropic.RateLimitError, 429),
        _status_error(anthropic.BadRequestError, 400),
        _status_error(anthropic.InternalServerError, 500),
        anthropic.APIConnectionError(request=httpx.Request("POST", "https://api.anthropic.com/v1/messages")),
    ],
)
async def test_other_api_errors_are_transient(exc: Exception) -> None:
    llm, _, _ = _llm(exc)
    with pytest.raises(LLMError) as info:
        await llm.complete(model="claude-haiku-5-5", system="S", content=[text_block("x")])
    assert not isinstance(info.value, LLMUnavailable)


async def test_non_api_errors_propagate_unchanged() -> None:
    llm, _, _ = _llm(ValueError("bug"))
    with pytest.raises(ValueError, match="bug"):
        await _structured(llm)


def test_missing_key_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    with pytest.raises(LLMUnavailable):
        ClaudeLLM()


async def test_aclose_closes_the_client() -> None:
    llm, client, _ = _llm()
    await llm.aclose()
    assert client.closed


# ------------------------------------------------------------ strict schema limits


def _nodes(node: Any) -> Iterator[dict[str, Any]]:
    if isinstance(node, dict):
        yield node
        for key, value in node.items():
            if key == "properties":  # a name -> schema map, not a schema itself
                for sub in value.values():
                    yield from _nodes(sub)
            else:
                yield from _nodes(value)
    elif isinstance(node, list):
        for value in node:
            yield from _nodes(value)


@pytest.mark.parametrize("model", [WireExtraction, WireSummary])
def test_wire_schemas_fit_strict_structured_output_limits(model: type) -> None:
    schema = transform_schema(model)
    nodes = list(_nodes(schema))
    objects = [n for n in nodes if n.get("type") == "object"]
    assert len(objects) >= 2  # the top level plus the nested item/update definitions
    optional = 0
    for obj in objects:
        assert obj.get("additionalProperties") is False, obj.get("title")
        assert sorted(obj["required"]) == sorted(obj["properties"]), obj.get("title")
        optional += len(set(obj["properties"]) - set(obj["required"]))
    assert optional == 0
    for node in nodes:
        assert not {"anyOf", "oneOf", "allOf"} & set(node), node
        assert not isinstance(node.get("type"), list), node  # nullable unions count against the limit
