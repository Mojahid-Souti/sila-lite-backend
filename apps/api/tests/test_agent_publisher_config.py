"""Slice 1a — the workspace card reads an editable publisher_config off the agent,
and the REST payload drops Expert-only fields for a Beginner (tier != privilege)."""

from __future__ import annotations

import datetime
import uuid
from types import SimpleNamespace

from sila.api.v1.agents import AgentOut, _status_label


def _agent(**over: object) -> SimpleNamespace:
    now = datetime.datetime.now(datetime.UTC)
    base = dict(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        name="Daily Cyber Summary Poster",
        description="A different section each day — posts to #all-silaagentstest.",
        owner_id=uuid.uuid4(),
        tags=[],
        status="draft",
        version=2,
        language="en",
        classification="internal",
        kind="workflow",
        vetting_status="approved",
        dify_app_id=None,
        created_at=now,
        updated_at=now,
        config={
            "schedule": {"cron": "0 9 * * *", "timezone": "Asia/Muscat"},
            "destinations": [{"type": "slack", "channel_name": "all-silaagentstest"}],
            "docs": ["Cybersecurity.pdf"],
        },
    )
    base.update(over)
    return SimpleNamespace(**base)


def test_status_label_mapping() -> None:
    assert _status_label("draft", "unvetted") == "Draft — pending review"
    assert _status_label("draft", "approved") == "Ready"
    assert _status_label("draft", "ready_for_review") == "Pending review"
    assert _status_label("published", "approved") == "Live"


def test_expert_sees_full_detail_and_config() -> None:
    out = AgentOut.from_orm(_agent(), "expert")
    assert out.vetting_status == "approved"
    assert out.version == 2
    assert out.status_label == "Ready"
    assert out.publisher_config and out.publisher_config["schedule"]["cron"] == "0 9 * * *"
    assert out.publisher_config["docs"] == ["Cybersecurity.pdf"]


def test_beginner_drops_expert_only_fields_but_keeps_friendly_status_and_config() -> None:
    out = AgentOut.from_orm(_agent(), "beginner")
    # Expert-only fields dropped server-side (not CSS):
    assert out.vetting_status is None
    assert out.version is None
    assert out.dify_app_id is None
    # Friendly status + the editable config remain (the card needs them):
    assert out.status_label == "Ready"
    assert out.publisher_config is not None
    assert out.publisher_config["destinations"][0]["channel_name"] == "all-silaagentstest"
