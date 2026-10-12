"""Agent endpoint unit tests — auth guard enforcement and YAML validation."""

import uuid
from collections.abc import Generator

import pytest
from fastapi.testclient import TestClient

from sila.core.utils.yaml_validator import validate_yaml
from sila.main import app

client = TestClient(app)

# Agent endpoints require a valid JWT — these tests verify auth guards and YAML validation.
# Full lifecycle integration tests live in scripts/test-phase-2.sh (make test-phase-2).


@pytest.fixture(autouse=True)
def clear_overrides() -> Generator[None]:
    yield
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------------
# Auth guard tests
# ---------------------------------------------------------------------------


def test_list_agents_unauthenticated() -> None:
    r = client.get(f"/v1/workspaces/{uuid.uuid4()}/agents")
    assert r.status_code in (401, 403)


def test_import_agent_yaml_unauthenticated() -> None:
    r = client.post(
        f"/v1/workspaces/{uuid.uuid4()}/agent-imports",
        files={"file": ("test.yaml", b"app:\n  name: test\n  mode: chat", "text/yaml")},
    )
    assert r.status_code in (401, 403)


def test_import_agent_template_unauthenticated() -> None:
    r = client.post(
        f"/v1/workspaces/{uuid.uuid4()}/agent-imports/template",
        json={"template_id": "customer_support"},
    )
    assert r.status_code in (401, 403)


def test_get_agent_unauthenticated() -> None:
    r = client.get(f"/v1/workspaces/{uuid.uuid4()}/agents/{uuid.uuid4()}")
    assert r.status_code in (401, 403)


def test_publish_agent_unauthenticated() -> None:
    r = client.post(f"/v1/workspaces/{uuid.uuid4()}/agents/{uuid.uuid4()}/publish")
    assert r.status_code in (401, 403)


def test_archive_agent_unauthenticated() -> None:
    r = client.post(f"/v1/workspaces/{uuid.uuid4()}/agents/{uuid.uuid4()}/archive")
    assert r.status_code in (401, 403)


def test_clone_agent_unauthenticated() -> None:
    r = client.post(f"/v1/workspaces/{uuid.uuid4()}/agents/{uuid.uuid4()}/clone")
    assert r.status_code in (401, 403)


def test_test_agent_unauthenticated() -> None:
    r = client.post(
        f"/v1/workspaces/{uuid.uuid4()}/agents/{uuid.uuid4()}/test",
        json={"query": "hello"},
    )
    assert r.status_code in (401, 403)


def test_list_versions_unauthenticated() -> None:
    r = client.get(f"/v1/workspaces/{uuid.uuid4()}/agents/{uuid.uuid4()}/versions")
    assert r.status_code in (401, 403)


def test_rollback_agent_unauthenticated() -> None:
    r = client.post(f"/v1/workspaces/{uuid.uuid4()}/agents/{uuid.uuid4()}/versions/1/rollback")
    assert r.status_code in (401, 403)


def test_list_templates_unauthenticated() -> None:
    r = client.get("/v1/templates")
    assert r.status_code in (401, 403)


# ---------------------------------------------------------------------------
# YAML validator unit tests (no DB required)
# ---------------------------------------------------------------------------


def test_yaml_validator_valid() -> None:
    raw = b"app:\n  name: My Agent\n  mode: chat\n"
    dsl, errors = validate_yaml(raw)
    assert errors == []
    assert dsl["app"]["name"] == "My Agent"


def test_yaml_validator_missing_name() -> None:
    raw = b"app:\n  mode: chat\n"
    _, errors = validate_yaml(raw)
    assert any("name" in e for e in errors)


def test_yaml_validator_invalid_mode() -> None:
    raw = b"app:\n  name: My Agent\n  mode: illegal\n"
    _, errors = validate_yaml(raw)
    assert any("mode" in e for e in errors)


def test_yaml_validator_name_too_short() -> None:
    raw = b"app:\n  name: x\n  mode: chat\n"
    _, errors = validate_yaml(raw)
    assert any("name" in e for e in errors)


def test_yaml_validator_exceeds_size() -> None:
    raw = b"x" * (1_048_576 + 1)
    _, errors = validate_yaml(raw)
    assert any("1 MiB" in e for e in errors)


def test_yaml_validator_malformed() -> None:
    raw = b"app: [\nunclosed"
    _, errors = validate_yaml(raw)
    assert any("YAML" in e for e in errors)


def test_yaml_validator_forbidden_python_node() -> None:
    raw = b"app:\n  name: Hack Agent\n  mode: chat\n__python__: bad\n"
    _, errors = validate_yaml(raw)
    assert any("__python__" in e for e in errors)


def test_yaml_validator_tool_not_in_allowlist() -> None:
    raw = b"app:\n  name: Tool Agent\n  mode: chat\ntool_configurations:\n  - tool_id: some-tool\n"
    _, errors = validate_yaml(raw, workspace_tool_ids=frozenset())
    assert any("some-tool" in e for e in errors)


def test_yaml_validator_tool_in_allowlist() -> None:
    raw = (
        b"app:\n  name: Tool Agent\n  mode: chat\n"
        b"tool_configurations:\n  - tool_id: registered-tool\n"
    )
    _, errors = validate_yaml(raw, workspace_tool_ids=frozenset({"registered-tool"}))
    assert errors == []


def test_yaml_validator_root_not_mapping() -> None:
    raw = b"- item1\n- item2\n"
    _, errors = validate_yaml(raw)
    assert any("mapping" in e for e in errors)
