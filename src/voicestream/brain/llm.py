"""Thin Claude client: structured (JSON-schema) and free-text completions with
prompt caching, per-model thinking/effort settings and usage accounting.

Notes on the Claude 5.x API (checked 2026-10):

* Structured output uses ``output_config.format`` (GA, no beta header). Forced
  ``tool_choice`` is rejected on Opus/Sonnet 5.5, so we don't rely on it.
* ``temperature``/``top_p`` are not accepted by the 1.x SDK.
* Thinking: Haiku 5.5 may disable it; Sonnet 5.5 uses ``between_tools`` as its
  cheapest mode; Opus 5.5 always thinks (omit the param).
* Strict schemas allow at most 24 optional and 16 union-typed fields, so the
  wire schemas in :mod:`voicestream.brain.schema` make every field required.
"""

from __future__ import annotations

import base64
import json
import logging
import re
from abc import ABC, abstractmethod
from collections.abc import Callable, Sequence
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..costs import TokenUsage

log = logging.getLogger(__name__)

M = TypeVar("M", bound=BaseModel)
Block = dict[str, Any]
UsageHook = Callable[[TokenUsage], Any]


class LLMError(RuntimeError):
    """The model call failed in a way the caller should handle (skip / fall back)."""


class LLMUnavailable(LLMError):
    """No credentials / SDK; permanent for this process."""


def text_block(text: str, cache: bool = False) -> Block:
    block: Block = {"type": "text", "text": text}
    if cache:
        block["cache_control"] = {"type": "ephemeral"}
    return block


def image_block(data: bytes, media_type: str = "image/jpeg") -> Block:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": media_type, "data": base64.standard_b64encode(data).decode()},
    }


class LLM(ABC):
    def __init__(self, on_usage: UsageHook | None = None) -> None:
        self.on_usage = on_usage

    @abstractmethod
    async def structured(
        self,
        *,
        model: str,
        system: str,
        content: Sequence[Block],
        schema: type[M],
        max_tokens: int = 4096,
        effort: str | None = "low",
        usage_hook: UsageHook | None = None,
    ) -> M: ...

    @abstractmethod
    async def complete(
        self,
        *,
        model: str,
        system: str,
        content: Sequence[Block],
        max_tokens: int = 4096,
        effort: str | None = "low",
        usage_hook: UsageHook | None = None,
    ) -> str: ...

    async def aclose(self) -> None:
        pass

    def _report(self, usage: TokenUsage, hook: UsageHook | None = None) -> None:
        """Send ``usage`` to the call's own ``usage_hook`` if it has one (agent runs are billed to the
        agent budget, not the extraction one), else to ``on_usage``."""
        hook = hook or self.on_usage
        if hook:
            try:
                hook(usage)
            except Exception:  # pragma: no cover
                log.exception("usage hook failed")


def model_params(model: str, effort: str | None) -> dict[str, Any]:
    """Cheapest sensible thinking/effort settings for the model family."""
    m = model.lower()
    params: dict[str, Any] = {}
    ver = re.search(r"claude-[a-z]+-(\d+)", m)
    is_5x = bool(ver) and int(ver.group(1)) >= 5
    if effort and is_5x:
        params["output_config"] = {"effort": effort}
    if not is_5x:
        return params
    if "haiku-5" in m:
        if effort in ("low", "medium", "high", None):
            params["thinking"] = {"type": "disabled"}
    elif "sonnet-5" in m:
        if effort in ("low", "medium", "high", None):
            params["thinking"] = {"type": "between_tools"}
    # Opus / Fable 5.x: thinking is always on; omit the parameter.
    return params


class ClaudeLLM(LLM):
    def __init__(
        self,
        api_key: str | None = None,
        on_usage: UsageHook | None = None,
        max_retries: int = 4,
        timeout: float = 90.0,
        client: Any = None,
    ) -> None:
        super().__init__(on_usage)
        if client is not None:
            self.client = client
            return
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - it's a core dependency
            raise LLMUnavailable("the 'anthropic' package is not installed") from exc
        import os

        key = api_key or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        if not key:
            raise LLMUnavailable("ANTHROPIC_API_KEY is not set")
        self.client = anthropic.AsyncAnthropic(api_key=key, max_retries=max_retries, timeout=timeout)

    async def aclose(self) -> None:
        close = getattr(self.client, "close", None)
        if close is not None:
            try:
                await close()
            except Exception:  # pragma: no cover
                pass

    async def _create(self, **kwargs: Any) -> Any:
        try:
            import anthropic
        except ImportError:  # pragma: no cover
            anthropic = None  # type: ignore[assignment]
        try:
            return await self.client.messages.create(**kwargs)
        except Exception as exc:
            if anthropic is not None and isinstance(exc, (anthropic.AuthenticationError, anthropic.PermissionDeniedError)):
                raise LLMUnavailable(f"Claude authentication failed: {exc}") from exc
            if anthropic is not None and getattr(exc, "status_code", None) == 402:
                raise LLMUnavailable(f"Claude billing error: {exc}") from exc
            if anthropic is not None and isinstance(exc, anthropic.BadRequestError):
                # 400s are bugs or unsupported params: surface loudly, don't retry
                log.error("Claude rejected the request: %s", getattr(exc, "message", exc))
            if anthropic is not None and isinstance(exc, anthropic.APIError):
                raise LLMError(f"{type(exc).__name__}: {getattr(exc, 'message', exc)}") from exc
            raise

    def _account(self, model: str, resp: Any, hook: UsageHook | None) -> None:
        usage = getattr(resp, "usage", None)
        if usage is not None:
            self._report(TokenUsage.from_anthropic(model, usage), hook)

    @staticmethod
    def _text_of(resp: Any) -> str:
        return "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", None) == "text")

    async def structured(
        self,
        *,
        model: str,
        system: str,
        content: Sequence[Block],
        schema: type[M],
        max_tokens: int = 4096,
        effort: str | None = "low",
        usage_hook: UsageHook | None = None,
    ) -> M:
        from anthropic import transform_schema

        params = model_params(model, effort)
        output_config = dict(params.pop("output_config", {}))
        output_config["format"] = {"type": "json_schema", "schema": transform_schema(schema)}
        resp = await self._create(
            model=model,
            max_tokens=max_tokens,
            system=[text_block(system, cache=True)],
            messages=[{"role": "user", "content": list(content)}],
            output_config=output_config,
            **params,
        )
        self._account(model, resp, usage_hook)
        stop = getattr(resp, "stop_reason", None)
        if stop == "refusal":
            raise LLMError(f"model refused: {getattr(resp, 'stop_details', None)}")
        if stop == "max_tokens":
            raise LLMError("structured output truncated (max_tokens); raise brain.max_output_tokens")
        raw = self._text_of(resp)
        try:
            return schema.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValidationError) as exc:
            raise LLMError(f"invalid structured output: {exc}; got {raw[:300]!r}") from exc

    async def complete(
        self,
        *,
        model: str,
        system: str,
        content: Sequence[Block],
        max_tokens: int = 4096,
        effort: str | None = "low",
        usage_hook: UsageHook | None = None,
    ) -> str:
        resp = await self._create(
            model=model,
            max_tokens=max_tokens,
            system=[text_block(system, cache=True)],
            messages=[{"role": "user", "content": list(content)}],
            **model_params(model, effort),
        )
        self._account(model, resp, usage_hook)
        if getattr(resp, "stop_reason", None) == "refusal":
            raise LLMError("model refused")
        return self._text_of(resp).strip()


class ScriptedLLM(LLM):
    """Test double: returns queued responses (objects/dicts/strings) and records calls."""

    def __init__(self, responses: Sequence[Any] = (), on_usage: UsageHook | None = None) -> None:
        super().__init__(on_usage)
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def _next(self) -> Any:
        if not self.responses:
            raise LLMError("ScriptedLLM: no responses left")
        nxt = self.responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    async def structured(  # type: ignore[override]
        self, *, model, system, content, schema, max_tokens=4096, effort="low", usage_hook=None
    ):
        self.calls.append({"kind": "structured", "model": model, "system": system, "content": list(content), "schema": schema})
        self._report(TokenUsage(model=model, input_tokens=1000, output_tokens=100), usage_hook)
        nxt = self._next()
        if callable(nxt) and not isinstance(nxt, BaseModel):
            nxt = nxt(self.calls[-1])
        if isinstance(nxt, BaseModel):
            return nxt if isinstance(nxt, schema) else schema.model_validate(nxt.model_dump())
        return schema.model_validate(nxt)

    async def complete(self, *, model, system, content, max_tokens=4096, effort="low", usage_hook=None):  # type: ignore[override]
        self.calls.append({"kind": "complete", "model": model, "system": system, "content": list(content)})
        self._report(TokenUsage(model=model, input_tokens=1000, output_tokens=100), usage_hook)
        nxt = self._next()
        if callable(nxt):
            nxt = nxt(self.calls[-1])
        return str(nxt)
