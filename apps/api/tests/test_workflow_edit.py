"""Slice 1b — rebuild the n8n definition from a publisher_config (the shared assembly
the edit path uses). Email-only configs need no DB (no Slack binding lookup)."""

from __future__ import annotations

import uuid

from sila.core.utils.workflow_validator import validate_workflow
from sila.services.sila.workflow_assembly import build_definition_from_config


def _types(defn: dict) -> list[str]:
    return [n["type"] for n in defn["nodes"]]


async def test_assembly_email_only_scheduled() -> None:
    cfg = {
        "schedule": {"cron": "0 9 * * 1-5", "timezone": "Asia/Muscat"},
        "destinations": [{"type": "email", "to": "me@sila.local"}],
        "kb_id": str(uuid.uuid4()),
        "content": "A different section each day",
        "model": "qwen2.5:7b-instruct",
    }
    defn = await build_definition_from_config(
        db=None, user_id=uuid.uuid4(), workspace_id=uuid.uuid4(), name="Edited Agent", config=cfg
    )
    types = _types(defn)
    # schedule trigger → Generate summary (Sila) → Email (Sila); NO langchain node.
    assert types[0] == "n8n-nodes-base.scheduleTrigger"
    assert types.count("n8n-nodes-base.httpRequest") == 2  # run + email
    assert not any("langchain" in t for t in types)
    assert defn["active"] is False
    email = next(n for n in defn["nodes"] if n["name"] == "Email summary")
    hdr = email["parameters"]["headerParameters"]["parameters"][0]
    assert hdr["name"] == "X-Sila-Email-Token" and hdr["value"]  # capability baked in
    # The schedule cron reflects the edit.
    sched = defn["nodes"][0]
    assert sched["parameters"]["rule"]["interval"][0]["expression"] == "0 9 * * 1-5"
    assert validate_workflow(defn)[1] == []


async def test_assembly_no_kb_no_run_node() -> None:
    cfg = {"destinations": [{"type": "email", "to": "x@y.dev"}], "schedule": {"cron": "0 9 * * *"}}
    defn = await build_definition_from_config(
        db=None, user_id=uuid.uuid4(), workspace_id=uuid.uuid4(), name="X", config=cfg
    )
    assert not any(n["name"] == "Generate summary" for n in defn["nodes"])  # no kb → no run node
    assert any(n["name"] == "Email summary" for n in defn["nodes"])
