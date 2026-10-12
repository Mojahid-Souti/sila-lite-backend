"""The email confirm gate — the draft must survive a reconnect, and the Send
sentinel must never reach the model.

Reproduces the reported loop: draft to a real recipient → reconnect → Send →
SILA answered with an invented draft to recipient@example.com, which then asked
to be confirmed too, forever.
"""

import re
import uuid
from pathlib import Path

import pytest

from sila.services.sila.orchestrator.graph import _CONFIRM_EMAIL_SENTINEL
from sila.services.sila.orchestrator.runner import _DURABLE_STATE_KEYS
from sila.services.sila.tool_registry import TOOLS


def test_pending_email_survives_a_reconnect() -> None:
    """The stash is rehydrated from the session row by key. If `pending_email`
    is not in that list the draft dies with the connection, the confirm gate
    finds nothing, and the sentinel falls through to the model."""
    assert "pending_email" in _DURABLE_STATE_KEYS


def test_email_confirmed_is_not_persisted() -> None:
    """Deliberate: the pause gate sets it and confirm_send_email reads it inside
    the same run. Persisting it would let a stale True from an earlier draft
    send mail the user never clicked Send on."""
    assert "email_confirmed" not in _DURABLE_STATE_KEYS


@pytest.mark.asyncio
async def test_confirm_without_a_draft_sends_nothing() -> None:
    """The hidden confirm tool is the only thing that delivers. With no stashed
    draft — or a draft the user never confirmed — it must refuse."""
    from unittest.mock import AsyncMock

    from sila.adapters.auth.base import UserResult
    from sila.core.constants import Role
    from sila.services.sila.tool_registry import ToolContext, execute_tool

    ctx = ToolContext(
        db=AsyncMock(),
        user=UserResult(id=uuid.uuid4(), email="u@x.io", email_confirmed=True),
        workspace_id=uuid.uuid4(),
        role=Role.ADMIN,
    )
    ctx.build_state = {}
    res = await execute_tool("confirm_send_email", ctx, {})
    assert "no confirmed email draft" in res.content.lower()

    # A draft the user never confirmed must not go either — having the stash is
    # not consent; only the pause gate sets email_confirmed.
    ctx.build_state = {"pending_email": {"to": "a@b.c", "subject": "s", "body": "b"}}
    res = await execute_tool("confirm_send_email", ctx, {})
    assert "no confirmed email draft" in res.content.lower()


def test_confirm_tool_stays_hidden_from_the_model() -> None:
    """The gate only works because the model cannot call the sender itself."""
    assert TOOLS["confirm_send_email"].extra.get("hidden") is True


def test_sentinel_is_a_single_shared_constant() -> None:
    """The WS handler and the pause gate must agree on the marker; two spellings
    would mean the gate silently never fires."""
    assert _CONFIRM_EMAIL_SENTINEL == "__confirm_email__"
    ws = Path("sila/api/v1/sila.py").read_text()
    assert _CONFIRM_EMAIL_SENTINEL in ws


def test_hire_agent_steers_build_requests_elsewhere() -> None:
    """'Build me an agent that…' was answered by hire_agent, which then had no
    id to hire and leaked its own error text to the user."""
    desc = TOOLS["hire_agent"].description.lower()
    assert "search_bazaar" in desc
    assert "build" in desc, "the description must say what to do with a build request"


@pytest.mark.asyncio
async def test_hire_agent_rejects_an_invented_id_without_user_facing_prose() -> None:
    from unittest.mock import AsyncMock

    from sila.adapters.auth.base import UserResult
    from sila.core.constants import Role
    from sila.services.sila.tool_registry import ToolContext, execute_tool

    ctx = ToolContext(
        db=AsyncMock(),
        user=UserResult(id=uuid.uuid4(), email="u@x.io", email_confirmed=True),
        workspace_id=uuid.uuid4(),
        role=Role.ADMIN,
    )
    ctx.build_state = {}
    res = await execute_tool("hire_agent", ctx, {"agent_id": "an-investment-agent"})
    assert "not hired" in res.content.lower()
    # It must tell the model not to relay it, and carry a failure summary so the
    # UI stops showing the optimistic "Hiring that agent…" line.
    assert "do not repeat this message" in res.content.lower()
    assert res.summary and "couldn't hire" in res.summary.lower()


def test_no_placeholder_recipient_anywhere_in_the_email_path() -> None:
    """The invented drafts used example.com addresses. Nothing in the send path
    may carry a default recipient a model could copy."""
    src = Path("sila/services/sila/tool_registry.py").read_text()
    assert not re.search(r"recipient@example\.com", src)


def test_tool_failures_never_hand_the_model_internal_names() -> None:
    """The crash/timeout fallbacks are relayed by the model almost verbatim, so
    the tool name and exception class must stay in the log and out of that text.
    "It seems there was an unexpected error with the search_bazaar function"
    reached a user because the fallback interpolated the name into it."""
    src = Path("sila/services/sila/tool_registry.py").read_text()
    block = src[src.index("except TimeoutError:") :][:1600]
    # The ToolResult content is what the model sees; the log line above it may
    # (and should) still carry the name.
    for line in block.splitlines():
        if "log.error" in line:
            continue
        assert "{name}" not in line, f"tool name reaches the model: {line.strip()}"
    assert block.count("Do NOT name tools") == 2, "both fallbacks must carry the rule"


def test_search_bazaar_passes_the_user_id_it_requires() -> None:
    """search_catalog takes user_id keyword-only (it drives the provenance
    split). The SILA tool omitted it and raised TypeError on every call."""
    import inspect

    from sila.services.bazaar_service import BazaarService

    assert "user_id" in inspect.signature(BazaarService.search_catalog).parameters
    src = Path("sila/services/sila/tool_registry.py").read_text()
    call = src[src.index("async def _search_bazaar") :][:2200]
    assert "user_id=ctx.user.id" in call


def test_session_and_memory_repos_do_not_commit_mid_request() -> None:
    """get_db wraps a request in one transaction. A repository that commits
    inside it closes that transaction, so the endpoint's audit row — and the
    dependency's own exit — then raise InvalidRequestError. This 500'd session
    delete and memory clear."""
    import re as _re

    src = Path("sila/db/repositories/sila.py").read_text()
    for meth in ("delete_session", "delete_sessions", "delete_memory", "clear_memories"):
        m = _re.search(rf"    async def {meth}\(.*?(?=\n    async def |\Z)", src, _re.S)
        assert m, meth
        assert "self._db.commit()" not in m.group(0), f"{meth} must not commit mid-request"


def test_sila_endpoints_do_not_commit() -> None:
    """The get_db docstring states it: endpoints never call db.commit()."""
    assert "await db.commit()" not in Path("sila/api/v1/sila.py").read_text()
