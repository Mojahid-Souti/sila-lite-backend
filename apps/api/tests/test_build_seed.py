"""Plan→Build handoff: the sanitized build seed pre-fills the interview.

The seed FILLS slots; it never bypasses the guard — the gate still walks the
slot list (so it skips what's seeded), and the user's explicit confirm is still
required before anything is built.
"""

from __future__ import annotations

import uuid

import pytest

from sila.adapters.auth.base import UserResult
from sila.api.v1.sila import _record_slot_answer
from sila.core.constants import Role
from sila.services.sila import build_slots as bs
from sila.services.sila.tool_registry import ToolContext, _requirements_gate

# --- sanitize_seed ----------------------------------------------------------


def test_valid_seed_passes_whitelisted_slots() -> None:
    seed = bs.sanitize_seed(
        {
            "agent_type": bs.SCHEDULED_PUBLISHER,
            "filled": {"content": "A summary of newly-added documents", "schedule": "0 9 * * 1-5"},
        }
    )
    assert seed["agent_type"] == bs.SCHEDULED_PUBLISHER
    assert seed["filled"] == {
        "content": "A summary of newly-added documents",
        "schedule": "0 9 * * 1-5",
    }


def test_unknown_agent_type_drops_whole_seed() -> None:
    assert bs.sanitize_seed({"agent_type": "root_shell", "filled": {"content": "x"}}) == {}


def test_unknown_and_cross_type_slots_dropped() -> None:
    seed = bs.sanitize_seed(
        {
            "agent_type": bs.KNOWLEDGE,
            "filled": {
                "out_of_scope": "Say it doesn't know",
                "schedule": "0 9 * * *",  # a publisher slot — not valid for knowledge
                "__proto__": "poison",
                "confirmed": "yes",  # not a slot — must never flip gate flags
            },
        }
    )
    assert seed["filled"] == {"out_of_scope": "Say it doesn't know"}


def test_values_length_capped_and_non_strings_coerced_or_dropped() -> None:
    seed = bs.sanitize_seed(
        {
            "agent_type": bs.KNOWLEDGE,
            "filled": {"out_of_scope": "x" * 5000, "escalation": ["a", "list"]},
        }
    )
    assert len(seed["filled"]["out_of_scope"]) == 300
    assert "escalation" not in seed["filled"]


@pytest.mark.parametrize("garbage", [None, "seed", 42, [], {"filled": {"content": "x"}}])
def test_garbage_seed_is_empty(garbage: object) -> None:
    assert bs.sanitize_seed(garbage) == {}


# --- the gate with a seeded state --------------------------------------------


def _ctx(build_state: dict) -> ToolContext:
    return ToolContext(
        db=None,  # type: ignore[arg-type]
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        workspace_id=uuid.uuid4(),
        role=Role.BUILDER,
        interactive=True,
        build_state=build_state,
    )


@pytest.mark.asyncio
async def test_seeded_interview_asks_docs_then_confirm_never_past_it() -> None:
    """A fully-seeded publisher asks NO slot questions — only the (skippable) docs
    upload, then the confirm. The confirm always gates."""
    state = bs.sanitize_seed(
        {
            "agent_type": bs.SCHEDULED_PUBLISHER,
            "filled": {"content": "A different section each day", "schedule": "0 9 * * 1-5"},
        }
    )
    ctx = _ctx(state)
    # 1st: the deterministic docs request (grounding), asked exactly once.
    g1 = await _requirements_gate(ctx, "Digest", "post daily summaries", [], None, "m")
    assert g1 is not None and g1.ui is not None and g1.ui["command"] == "open_modal"
    # 2nd: straight to the confirm — no requirement questions re-asked.
    g2 = await _requirements_gate(ctx, "Digest", "post daily summaries", [], None, "m")
    assert g2 is not None and g2.ui is not None
    assert g2.ui["questions"][0]["id"] == "__confirm__"
    # The user accepts → the gate opens; only now may the build proceed.
    _record_slot_answer(state, {"__confirm__": "Yes, build it"})
    assert await _requirements_gate(ctx, "Digest", "post daily summaries", [], None, "m") is None


@pytest.mark.asyncio
async def test_partial_seed_asks_only_whats_missing() -> None:
    """A partial seed (content only) asks the remaining slot(s), not the seeded one."""
    state = bs.sanitize_seed(
        {
            "agent_type": bs.SCHEDULED_PUBLISHER,
            "filled": {"content": "A different section each day"},
        }
    )
    state["docs_asked"] = True  # past the docs step — this test is about the slots
    ctx = _ctx(state)
    gate = await _requirements_gate(ctx, "Digest", "post daily summaries", [], None, "m")
    assert gate is not None and gate.ui is not None
    asked = {q["id"] for q in gate.ui["questions"]}
    assert "schedule" in asked
    assert "content" not in asked


def test_seed_carries_deterministic_build_args() -> None:
    """The Accept handoff must not depend on the model: the seed's build_args are
    validated and become the stashed args the pause node re-runs the build with."""
    seed = bs.sanitize_seed(
        {
            "agent_type": bs.SCHEDULED_PUBLISHER,
            "filled": {},
            "build_args": {
                "name": "Competitive Digest",
                "purpose": "Analyze competitors and post a digest",
                "integrations": ["Slack", "", 42],
            },
        }
    )
    assert seed["build_args"]["name"] == "Competitive Digest"
    assert seed["build_args"]["integrations"] == ["slack", "42"]
    # Garbage / incomplete build_args are dropped, never half-trusted.
    no_args = bs.sanitize_seed(
        {"agent_type": bs.KNOWLEDGE, "filled": {}, "build_args": {"name": "X"}}
    )
    assert "build_args" not in no_args
