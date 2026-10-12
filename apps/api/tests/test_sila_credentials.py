"""Stage B2 — credential wiring tests (pure, no n8n/DB).

Covers the two deterministic pieces: build_rag_workflow injecting a node credential
REFERENCE (id, never a secret), and the curated credential specs. The n8n/MCP calls
in credential_service.connect() are thin I/O wrappers, exercised live.
"""

from __future__ import annotations

import json

from sila.services.n8n_workflow_builder import WorkflowSpec, build_rag_workflow
from sila.services.sila.credential_service import fields_payload, spec_for


def _spec(**kw: object) -> WorkflowSpec:
    base = {"name": "HR", "pre_prompt": "p", "model": "m", "ollama_url": "http://o"}
    return WorkflowSpec(**{**base, **kw})  # type: ignore[arg-type]


def test_build_wires_credential_into_integration_node() -> None:
    wf = build_rag_workflow(
        _spec(integrations=["slack"], credentials={"slack": ("slackApi", "cred-123")})
    )
    slack = next(n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.slack")
    assert slack["credentials"] == {"slackApi": {"id": "cred-123", "name": "Post to Slack (SILA)"}}
    # The definition carries only the credential id/type — never a secret value.
    assert "cred-123" in json.dumps(wf)


def test_build_no_credentials_block_when_absent() -> None:
    wf = build_rag_workflow(_spec(integrations=["slack"]))
    slack = next(n for n in wf["nodes"] if n["type"] == "n8n-nodes-base.slack")
    assert "credentials" not in slack


def test_spec_for_slack_is_n8n_masked_token() -> None:
    spec = spec_for("slack")
    assert spec is not None
    assert spec.target == "n8n" and spec.n8n_type == "slackApi"
    assert fields_payload(spec) == [
        {"key": "accessToken", "label": "Slack bot token", "secret": True}
    ]


def test_spec_for_unknown_provider_is_none() -> None:
    assert spec_for("dropbox") is None


def test_mcp_spec_is_admin_only() -> None:
    spec = spec_for("mcp")
    assert spec is not None
    assert spec.admin_only is True and spec.target == "mcp"
