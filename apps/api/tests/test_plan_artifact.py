"""Plan-mode plan artifact: normalization, seed mapping, and the pausing tool.

The model proposes; the code disposes — every card field is validated/capped
here, and the seed is re-run through sanitize_seed. propose_plan never builds:
it pauses for the user's Accept/Adjust/Discard decision.
"""

from __future__ import annotations

import uuid

import pytest

from sila.adapters.auth.base import UserResult
from sila.core.constants import Role
from sila.services.sila import build_slots as bs
from sila.services.sila.concierge_service import _PAUSING_TOOLS, PLAN_TOOL_NAMES
from sila.services.sila.plan_artifact import build_plan
from sila.services.sila.tool_registry import (
    TOOLS,
    WORKER_TOOL_NAMES,
    ToolContext,
    _propose_plan,
)

# --- build_plan --------------------------------------------------------------


def test_publisher_plan_maps_slots_into_the_seed() -> None:
    plan, seed = build_plan(
        {
            "goal": "Post a daily summary of our policies to Slack",
            "agent_type": "scheduled_publisher",
            "content": "A summary of newly-added documents",
            "schedule": "Weekdays at 9:00 AM (Asia/Muscat)",
            "destination": "#announcements",
            "integrations": ["slack"],
        }
    )
    assert plan["agentType"] == "Scheduled publisher"
    assert seed["agent_type"] == bs.SCHEDULED_PUBLISHER
    assert seed["filled"]["content"] == "A summary of newly-added documents"
    assert seed["filled"]["schedule"] == "Weekdays at 9:00 AM (Asia/Muscat)"
    assert seed["filled"]["destination"] == "#announcements"


def test_email_destination_fills_email_to() -> None:
    _, seed = build_plan(
        {
            "goal": "Email me a digest every morning",
            "agent_type": "scheduled_publisher",
            "destination": "me@company.om",
            "integrations": ["email"],
        }
    )
    assert seed["filled"]["email_to"] == "me@company.om"
    assert "destination" not in seed["filled"]


def test_knowledge_plan_maps_fallback_and_escalation() -> None:
    _, seed = build_plan(
        {
            "goal": "Answer HR questions from the handbook",
            "agent_type": "knowledge",
            "fallback": "Say it doesn't know and point to a human",
            "escalation": "Disciplinary topics",
        }
    )
    assert seed["agent_type"] == bs.KNOWLEDGE
    assert seed["filled"]["out_of_scope"] == "Say it doesn't know and point to a human"
    assert seed["filled"]["escalation"] == "Disciplinary topics"


def test_invalid_agent_type_falls_back_to_classification() -> None:
    plan, seed = build_plan({"goal": "post a daily digest to slack", "agent_type": "root"})
    assert seed["agent_type"] == bs.SCHEDULED_PUBLISHER
    assert plan["agentType"] == "Scheduled publisher"


def test_empty_goal_yields_nothing() -> None:
    assert build_plan({"agent_type": "knowledge"}) == ({}, {})


def test_open_decisions_and_bazaar_match_validated() -> None:
    plan, _ = build_plan(
        {
            "goal": "Summarize tickets",
            "open_decisions": [
                {"question": "Which channel?", "options": ["#ops", "#dev"]},
                "garbage",
                {"options": ["no question"]},
            ],
            "bazaar_match": {"agent_id": "abc-123", "name": "Ticket Digest", "note": "vetted"},
        }
    )
    assert plan["openDecisions"] == [{"question": "Which channel?", "options": ["#ops", "#dev"]}]
    assert plan["bazaarMatch"] == {"id": "abc-123", "name": "Ticket Digest", "note": "vetted"}


def test_bazaar_match_requires_id_and_name() -> None:
    plan, _ = build_plan({"goal": "x", "bazaar_match": {"name": "No Id"}})
    assert plan["bazaarMatch"] is None


def test_deep_plan_sections_validated() -> None:
    plan, _ = build_plan(
        {
            "goal": "Competitive analysis pipeline",
            "steps": [
                {
                    "title": "Fetch open accounts from the CRM",
                    "detail": "Top 3 by deal size",
                    "input": "CRM query",
                    "output": "accounts[] {name, industry}",
                    "parallel": True,
                },
                {"title": "SWOT synthesis on scraped pages", "depends_on": "the fetch"},
                {"detail": "no title — dropped"},
                "garbage",
            ],
            "risks": [
                {"risk": "Model returns invalid JSON", "fallback": "Re-run once with the error"},
                {"risk": "No fallback — dropped"},
            ],
            "checkpoints": ["Approve drafted emails before anything is sent", "", 42],
        }
    )
    assert [s["title"] for s in plan["steps"]] == [
        "Fetch open accounts from the CRM",
        "SWOT synthesis on scraped pages",
    ]
    assert plan["steps"][0]["parallel"] is True
    assert plan["steps"][0]["output"] == "accounts[] {name, industry}"
    assert plan["steps"][1]["dependsOn"] == "the fetch"
    assert plan["risks"] == [
        {"risk": "Model returns invalid JSON", "fallback": "Re-run once with the error"}
    ]
    assert plan["checkpoints"] == ["Approve drafted emails before anything is sent", "42"]


def test_step_and_risk_counts_capped() -> None:
    plan, _ = build_plan(
        {
            "goal": "x",
            "steps": [{"title": f"t{i}"} for i in range(20)],
            "risks": [{"risk": f"r{i}", "fallback": "f"} for i in range(20)],
            "checkpoints": [f"c{i}" for i in range(20)],
        }
    )
    assert len(plan["steps"]) == 10
    assert len(plan["risks"]) == 6
    assert len(plan["checkpoints"]) == 5


# --- the propose_plan tool ----------------------------------------------------


def _ctx() -> ToolContext:
    return ToolContext(
        db=None,  # type: ignore[arg-type]
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        workspace_id=uuid.uuid4(),
        role=Role.VIEWER,
    )


@pytest.mark.asyncio
async def test_propose_plan_pauses_with_the_shared_request_id() -> None:
    result = await _propose_plan(_ctx(), {"goal": "Answer HR questions from the handbook"})
    assert result.ui is not None and result.ui["command"] == "ask_user"
    q = result.ui["questions"][0]
    assert q["id"] == "__plan__"
    assert q["options"] == ["Accept & build", "Adjust the plan", "Discard"]


@pytest.mark.asyncio
async def test_propose_plan_without_goal_does_not_pause() -> None:
    result = await _propose_plan(_ctx(), {})
    assert result.ui is None  # guidance back to the model; the turn continues


def test_plan_scope_and_registry_wiring() -> None:
    assert "propose_plan" in PLAN_TOOL_NAMES  # available in Plan / AUTO-plan behaviour
    assert "propose_plan" in _PAUSING_TOOLS  # the turn ends on the decision
    assert "propose_plan" in TOOLS
    assert "propose_plan" not in WORKER_TOOL_NAMES  # fan-out workers never plan/pause
    # The advisory scope still has no build tools.
    assert "create_agent_workflow" not in PLAN_TOOL_NAMES
