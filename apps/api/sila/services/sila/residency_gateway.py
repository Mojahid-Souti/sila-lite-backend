"""The chat's model gateway, with one rule: confidential conversations stay local.

`SILA_LLM_PROVIDER=qwen-cloud` points the concierge at a hosted model. That is
a process-wide choice, and a conversation carrying an OKR sheet (or pinned to
the OKR Reporting Pack) must not follow it: the sheet, the user's questions
about it, the planner's decomposition, the session title — every model call in
that conversation goes to the local Ollama, or nowhere.

The session holds one ResidencyGateway for its whole life. It routes each call:

  * confidential  -> the local gateway; if that is not local, or its model is
    not pulled, `ConfidentialRefusalError` — never the hosted one;
  * otherwise     -> the hosted gateway, after a last check of the outgoing
    text: an OKR header anywhere in it (a pasted sheet, a tool result) marks
    the conversation confidential first and the call goes local instead.

Confidential is sticky: once set, it is never cleared for that session.

Complexity: O(1) routing per call, plus O(length of the outgoing text) for the
backstop scan when the call would go to a hosted model.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import structlog

from sila.adapters.llm.base import LLMChunk, LLMGateway, LLMGatewayError
from sila.services.okr_optional import contains_okr_header

log = structlog.get_logger(__name__)

REFUSAL = (
    "This conversation carries an OKR sheet, which is confidential: it may only be "
    "processed by a model running on this server. No local model is available right "
    "now, so nothing was sent anywhere. Ask an administrator to start the local "
    "model, then send your message again."
)


class ConfidentialRefusalError(LLMGatewayError):
    """A confidential conversation, and no local model to send it to."""


class ResidencyGateway:
    """An LLMGateway that keeps confidential conversations on this server."""

    def __init__(self, hosted: LLMGateway, local: LLMGateway) -> None:
        self._hosted = hosted
        self._local = local
        self._confidential = False
        self._local_ready: bool | None = None
        # Told once, when the session turns confidential (the session state
        # records it, so tools such as `remember` can refuse).
        self.on_confidential: Callable[[], None] | None = None

    @property
    def confidential(self) -> bool:
        return self._confidential

    @property
    def is_local(self) -> bool:
        """Where the NEXT call goes (read by the OKR residency guard)."""
        return self._routes_local() and _declares_local(self._local)

    @property
    def engine(self) -> str:
        """For cost telemetry: which kind of endpoint served this session's calls."""
        return "ollama" if self._routes_local() else "hosted"

    def mark_confidential(self, reason: str) -> None:
        if self._confidential:
            return
        log.info("sila.residency.confidential", reason=reason)
        self._confidential = True
        if self.on_confidential is not None:
            self.on_confidential()

    async def local_available(self) -> bool:
        """Is there a local model to send a confidential conversation to?"""
        if not _declares_local(self._local):
            return False
        if self._local_ready is not True:  # re-probe until it answers once
            probe = getattr(self._local, "is_available", None)
            self._local_ready = bool(await probe()) if probe is not None else False
        return self._local_ready

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        timeout_s: float = 30.0,
        schema: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        gateway = await self._route(system_prompt, user_prompt)
        return await gateway.complete_json(
            system_prompt=system_prompt, user_prompt=user_prompt, timeout_s=timeout_s, schema=schema
        )

    async def stream_chat(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        timeout_s: float = 120.0,
    ) -> AsyncIterator[LLMChunk]:
        gateway = await self._route(*(_text_of(m) for m in messages))
        async for chunk in gateway.stream_chat(messages=messages, tools=tools, timeout_s=timeout_s):
            yield chunk

    def _routes_local(self) -> bool:
        return self._confidential or _declares_local(self._hosted)

    async def _route(self, *texts: str) -> LLMGateway:
        hosted_call = not self._confidential and not _declares_local(self._hosted)
        if hosted_call and any(contains_okr_header(t) for t in texts):
            self.mark_confidential("okr header in outgoing text")
        if not self._routes_local():
            return self._hosted
        if self._confidential and not await self.local_available():
            raise ConfidentialRefusalError(REFUSAL)
        return self._local if self._confidential else self._hosted


def _declares_local(gateway: object) -> bool:
    """The adapter's own answer; anything that does not say True is hosted."""
    return getattr(gateway, "is_local", False) is True


def _text_of(message: dict[str, Any]) -> str:
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):  # multi-part content
        return "\n".join(str(p.get("text", "")) for p in content if isinstance(p, dict))
    return ""
