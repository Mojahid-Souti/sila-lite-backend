"""Publish-to-Bazaar + hire provisioning — privacy sanitization, the
fail-closed validation order, and the whitelist config merge.

Unit style (repo convention): no live DB — repos/adapters are mocked at the
hire_provisioning module seams; the sanitizer and payload tests are pure.
"""

import uuid
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from sila.main import app
from sila.services import hire_provisioning
from sila.services.bazaar_service import BazaarService, _requirements_from_config

client = TestClient(app)

# Keys that must NEVER appear anywhere in a bazaar payload (publisher targets).
_FORBIDDEN_KEYS = {
    "to",
    "channel_id",
    "channel_name",
    "spreadsheet_id",
    "space",
    "kb_id",
    "docs",
    "teams_target",
    "excel_link",
    "clickup_list",
}

_LOADED_CONFIG: dict[str, Any] = {
    "schedule": {"cron": "0 9 * * 1-5", "timezone": "Asia/Muscat"},
    "destinations": [
        {"type": "slack", "channel_id": "C123", "channel_name": "secret-ops"},
        {"type": "gmail", "to": "boss@omantel.om"},
        {"type": "email", "to": "me@omantel.om"},
    ],
    "kb_id": str(uuid.uuid4()),
    "docs": ["Q3-financials.pdf", "board-minutes.docx"],
    "content": "A daily summary of the Q3 records",
    "model": "qwen2.5:7b-instruct",
    "language": "en",
}


def _assert_no_forbidden_keys(value: object, path: str = "$") -> None:
    if isinstance(value, dict):
        for k, v in value.items():
            assert k not in _FORBIDDEN_KEYS, f"forbidden key '{k}' leaked at {path}"
            _assert_no_forbidden_keys(v, f"{path}.{k}")
    elif isinstance(value, list):
        for i, item in enumerate(value):
            _assert_no_forbidden_keys(item, f"{path}[{i}]")


# ---------------------------------------------------------------------------
# Sanitized requirements (pure)
# ---------------------------------------------------------------------------


def test_requirements_are_target_free() -> None:
    req = _requirements_from_config(_LOADED_CONFIG)
    assert req is not None
    _assert_no_forbidden_keys(req)
    assert req["requires"] == ["gmail", "slack"]  # oauth providers only, sorted
    assert req["needs_email_recipient"] is True  # platform SMTP needs a recipient
    assert req["docs_count"] == 2  # count, never filenames
    assert req["data_mode"] == "own_docs"
    assert req["schedule"] == {"cron": "0 9 * * 1-5", "timezone": "Asia/Muscat"}


def test_requirements_data_mode_included_when_publisher_opted_in() -> None:
    req = _requirements_from_config({**_LOADED_CONFIG, "share_docs": True})
    assert req is not None and req["data_mode"] == "included"


def test_requirements_none_for_chat_or_empty() -> None:
    assert _requirements_from_config(None) is None
    assert _requirements_from_config({}) is None


def test_bazaar_payload_never_leaks_publisher_targets() -> None:
    agent = MagicMock()
    agent.id = uuid.uuid4()
    agent.name = "Q3 Analyzer"
    agent.description = "d"
    agent.tags = []
    agent.status = "published"
    agent.language = "en"
    agent.classification = "internal"
    agent.kind = "workflow"
    agent.liked_count = 0
    agent.disliked_count = 0
    agent.owner_id = uuid.uuid4()
    agent.workspace_id = uuid.uuid4()
    agent.version = 1
    agent.created_at = datetime.now(UTC)
    agent.updated_at = datetime.now(UTC)
    agent.config = _LOADED_CONFIG

    svc = BazaarService(MagicMock(), MagicMock())
    payload = svc._agent_to_dict(agent, hired_ids=set())
    _assert_no_forbidden_keys(payload)
    assert payload["requirements"]["requires"] == ["gmail", "slack"]  # type: ignore[index]


# ---------------------------------------------------------------------------
# Provision endpoint — auth guard
# ---------------------------------------------------------------------------


def test_provision_unauthenticated() -> None:
    r = client.post(
        f"/v1/workspaces/{uuid.uuid4()}/hires/{uuid.uuid4()}/provision",
        json={"destinations": [{"type": "email", "to": "a@b.c"}]},
    )
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# provision_hire service — fail-closed validation + whitelist merge
# ---------------------------------------------------------------------------


def _source_agent(config: dict[str, Any] | None = None) -> MagicMock:
    agent = MagicMock()
    agent.id = uuid.uuid4()
    agent.name = "Daily Digest"
    agent.workspace_id = uuid.uuid4()
    agent.kind = "workflow"
    agent.config = config if config is not None else dict(_LOADED_CONFIG)
    return agent


@pytest.fixture
def seams(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Mock every side-effect seam; return the mocks for assertions."""
    bindings = MagicMock()
    bindings.get = AsyncMock(return_value=MagicMock())  # bound by default
    monkeypatch.setattr(hire_provisioning, "ConnectorBindingRepository", lambda db: bindings)

    kb_repo = MagicMock()
    kb_repo.get = AsyncMock(return_value=MagicMock())
    monkeypatch.setattr(hire_provisioning, "KBRepository", lambda db: kb_repo)

    doc = MagicMock()
    doc.filename = "mine.pdf"
    docs_repo = MagicMock()
    docs_repo.list_by_kb = AsyncMock(return_value=([doc], 1))
    monkeypatch.setattr(hire_provisioning, "DocumentRepository", lambda db: docs_repo)

    kb_svc = MagicMock()
    kb_svc.create_kb = AsyncMock(return_value={"id": str(uuid.uuid4())})
    kb_svc.copy_documents = AsyncMock(return_value=["Q3-financials.pdf"])
    monkeypatch.setattr(hire_provisioning, "build_kb_service", lambda db: kb_svc)

    build_def = AsyncMock(return_value={"name": "x", "nodes": []})
    import sila.services.sila.workflow_assembly as wa

    monkeypatch.setattr(wa, "build_definition_from_config", build_def)

    session = MagicMock()
    session.cookie_value = "cookie"
    monkeypatch.setattr(
        hire_provisioning.n8n_provisioning, "ensure_session", AsyncMock(return_value=session)
    )

    hire_row = MagicMock()
    hire_row.id = uuid.uuid4()
    hires = MagicMock()
    hires.hire = AsyncMock(return_value=hire_row)
    monkeypatch.setattr(hire_provisioning, "HireRepository", lambda db: hires)

    audit = MagicMock()
    audit.record = AsyncMock()
    monkeypatch.setattr(hire_provisioning, "AuditRepository", lambda db: audit)

    n8n = MagicMock()
    n8n.create_workflow = AsyncMock(return_value="wf-77")
    agent_service = MagicMock()
    new_agent = MagicMock()
    new_agent.id = uuid.uuid4()
    agent_service.register_workflow_agent = AsyncMock(return_value=new_agent)

    return {
        "bindings": bindings,
        "kb_repo": kb_repo,
        "kb_svc": kb_svc,
        "build_def": build_def,
        "n8n": n8n,
        "agent_service": agent_service,
        "hires": hires,
        "audit": audit,
        "new_agent": new_agent,
    }


async def _provision(seams: dict[str, Any], source: MagicMock, **overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "db": MagicMock(),
        "n8n": seams["n8n"],
        "agent_service": seams["agent_service"],
        "source": source,
        "hirer_user_id": uuid.uuid4(),
        "hirer_workspace_id": uuid.uuid4(),
        "destinations": [{"type": "slack", "channel_id": "C-HIRER", "channel_name": "mine"}],
        "schedule": None,
        "kb_id": uuid.uuid4(),
        "name": None,
    }
    kwargs.update(overrides)
    return await hire_provisioning.provision_hire(**kwargs)


async def test_happy_path_merges_only_hirer_data(seams: dict[str, Any]) -> None:
    source = _source_agent()
    agent, hire, warnings = await _provision(seams, source)

    merged = seams["build_def"].await_args.kwargs["config"]
    # The hirer's inputs won; nothing of the publisher's targets crossed.
    assert merged["destinations"] == [
        {"type": "slack", "channel_id": "C-HIRER", "channel_name": "mine"}
    ]
    assert merged["kb_id"] != _LOADED_CONFIG["kb_id"]
    assert merged["docs"] == ["mine.pdf"]
    # The recipe (content/model/language/schedule) came along verbatim.
    assert merged["content"] == _LOADED_CONFIG["content"]
    assert merged["model"] == _LOADED_CONFIG["model"]
    assert merged["schedule"] == _LOADED_CONFIG["schedule"]
    assert merged["source_agent_id"] == str(source.id)

    seams["n8n"].create_workflow.assert_awaited_once()
    register = seams["agent_service"].register_workflow_agent
    assert register.await_args.kwargs["workflow_id"] == "wf-77"
    assert agent is seams["new_agent"]
    seams["hires"].hire.assert_awaited_once()
    audit_kwargs = seams["audit"].record.await_args.kwargs
    assert audit_kwargs["action"] == "agent.hire"
    assert audit_kwargs["metadata"]["source_agent_id"] == str(source.id)


async def test_missing_binding_fails_before_any_side_effect(seams: dict[str, Any]) -> None:
    seams["bindings"].get = AsyncMock(return_value=None)
    with pytest.raises(HTTPException) as exc:
        await _provision(seams, _source_agent())
    assert exc.value.status_code == 400
    assert "Slack" in exc.value.detail
    seams["n8n"].create_workflow.assert_not_awaited()
    seams["kb_svc"].create_kb.assert_not_awaited()
    seams["agent_service"].register_workflow_agent.assert_not_awaited()


async def test_unsupported_destination_type_400(seams: dict[str, Any]) -> None:
    with pytest.raises(HTTPException) as exc:
        await _provision(
            seams, _source_agent(), destinations=[{"type": "ms-teams", "teams_target": "t/c"}]
        )
    assert exc.value.status_code == 400 and "ms-teams" in exc.value.detail


async def test_missing_target_field_400(seams: dict[str, Any]) -> None:
    with pytest.raises(HTTPException) as exc:
        await _provision(seams, _source_agent(), destinations=[{"type": "gmail"}])
    assert exc.value.status_code == 400 and "'to'" in exc.value.detail


async def test_foreign_kb_400(seams: dict[str, Any]) -> None:
    seams["kb_repo"].get = AsyncMock(return_value=None)
    with pytest.raises(HTTPException) as exc:
        await _provision(seams, _source_agent())
    assert exc.value.status_code == 400
    assert "Knowledge base" in exc.value.detail


async def test_included_data_copies_docs_into_hirer_kb(seams: dict[str, Any]) -> None:
    source = _source_agent({**_LOADED_CONFIG, "share_docs": True})
    _agent, _hire, warnings = await _provision(seams, source, kb_id=None)

    copy = seams["kb_svc"].copy_documents
    copy.assert_awaited_once()
    assert str(copy.await_args.kwargs["source_kb_id"]) == _LOADED_CONFIG["kb_id"]
    assert copy.await_args.kwargs["source_workspace_id"] == source.workspace_id
    merged = seams["build_def"].await_args.kwargs["config"]
    assert merged["kb_id"] == str(copy.await_args.kwargs["dest_kb_id"])
    assert merged["docs"] == ["Q3-financials.pdf"]
    assert any("processed" in w for w in warnings)


async def test_no_kb_warns_but_provisions(seams: dict[str, Any]) -> None:
    _agent, _hire, warnings = await _provision(seams, _source_agent(), kb_id=None)
    assert any("without your documents" in w for w in warnings)
    seams["agent_service"].register_workflow_agent.assert_awaited_once()
