"""Tests for the RAG n8n workflow assembler (services/n8n_workflow_builder).

The RAG (KB retrieval + LLM summary) runs in Sila via a capability-authed
httpRequest to the internal agent-run endpoint — the langchain lmChatOllama node
can't execute on n8n's main flow. So the workflow is executable main-flow nodes
only: trigger → Generate summary (httpRequest → Sila) → integration post(s) →
(Respond, webhook only).
"""

from __future__ import annotations

import json
from dataclasses import replace

from sila.core.utils.workflow_validator import validate_workflow
from sila.services.n8n_workflow_builder import (
    NODE_HTTP,
    NODE_RESPOND,
    NODE_SCHEDULE,
    NODE_WEBHOOK,
    ConnectorNode,
    WorkflowSpec,
    build_rag_workflow,
)

_BASE = WorkflowSpec(
    name="HR Assistant",
    pre_prompt="You are HR Assistant.",
    model="qwen2.5:7b-instruct",
    ollama_url="http://ollama:11434",
    kb_search_url="http://api:8000/v1/workspaces/w/kbs/k/search",
    run_url="http://sila-api:8001/v1/internal/agents/run",
    run_token="run.sig",
)


def _types(definition: dict) -> list[str]:
    return [n["type"] for n in definition["nodes"]]


def test_webhook_chain_shape() -> None:
    wf = build_rag_workflow(_BASE)
    # RAG runs in Sila → ONE httpRequest "Generate summary" node, NO langchain node.
    assert _types(wf) == [NODE_WEBHOOK, NODE_HTTP, NODE_RESPOND]
    assert "@n8n/n8n-nodes-langchain.lmChatOllama" not in _types(wf)
    assert wf["active"] is False
    # Linear connections: each non-terminal node points to the next.
    names = [n["name"] for n in wf["nodes"]]
    for src, dst in zip(names, names[1:], strict=False):
        assert wf["connections"][src]["main"][0][0]["node"] == dst
    _, errors = validate_workflow(wf)
    assert errors == []


def test_scheduled_publisher_uses_schedule_trigger_no_respond() -> None:
    wf = build_rag_workflow(replace(_BASE, schedule_cron="0 9 * * *"))
    types = _types(wf)
    assert types[0] == NODE_SCHEDULE
    assert NODE_WEBHOOK not in types
    assert NODE_RESPOND not in types  # a schedule doesn't reply to a caller
    sched = wf["nodes"][0]
    assert sched["parameters"]["rule"]["interval"][0]["expression"] == "0 9 * * *"
    _, errors = validate_workflow(wf)  # schedule trigger satisfies the validator
    assert errors == []


def test_generate_node_calls_sila_run_with_capability_header() -> None:
    gen = next(n for n in build_rag_workflow(_BASE)["nodes"] if n["name"] == "Generate summary")
    assert gen["type"] == NODE_HTTP
    assert gen["parameters"]["url"].endswith("/v1/internal/agents/run")
    header = gen["parameters"]["headerParameters"]["parameters"][0]
    assert header["name"] == "X-Sila-Run-Token"
    assert header["value"] == "run.sig"


def test_no_run_means_no_generate_node() -> None:
    wf = build_rag_workflow(replace(_BASE, run_url=None, run_token=None))
    assert _types(wf) == [NODE_WEBHOOK, NODE_RESPOND]


def test_no_code_nodes_ever() -> None:
    wf = build_rag_workflow(replace(_BASE, integrations=["email", "slack"]))
    assert not any("code" in t.lower() for t in _types(wf))


def test_integrations_inserted_before_respond() -> None:
    wf = build_rag_workflow(replace(_BASE, integrations=["email"]))
    types = _types(wf)
    assert types[-1] == NODE_RESPOND
    assert types[-2] == "n8n-nodes-base.emailSend"


# --- WS-9: OAuth connector node (Slack → Sila internal proxy) ---------------


def _slack_spec(*, message_field: str | None = None) -> WorkflowSpec:
    return replace(
        _BASE,
        integrations=["slack"],
        connectors={
            "slack": ConnectorNode(
                provider="slack",
                endpoint="chat.postMessage",
                data={"channel": "C123"},
                token="cap.sig",
                message_field=message_field,
            )
        },
        connector_proxy_url="http://sila-api:8001/v1/internal/connectors/proxy",
        connector_token="cap.sig",
    )


def test_oauth_connector_is_httprequest_to_proxy_no_credential() -> None:
    wf = build_rag_workflow(_slack_spec())
    assert "n8n-nodes-base.slack" not in _types(wf)
    slack = next(n for n in wf["nodes"] if "Slack" in n["name"])
    assert slack["type"] == NODE_HTTP
    assert "credentials" not in slack  # n8n holds NO third-party credential
    params = slack["parameters"]
    assert params["url"].endswith("/v1/internal/connectors/proxy")
    body = json.loads(params["jsonBody"])  # no message_field → static JSON body
    assert body["provider"] == "slack"
    assert body["endpoint"] == "chat.postMessage"
    assert body["data"]["channel"] == "C123"
    header = params["headerParameters"]["parameters"][0]
    assert header["name"] == "X-Sila-Connector-Token"
    assert header["value"] == "cap.sig"


def test_oauth_connector_injects_generated_summary_expression() -> None:
    # The production path: the action's message_field receives the generated summary.
    wf = build_rag_workflow(_slack_spec(message_field="text"))
    slack = next(n for n in wf["nodes"] if "Slack" in n["name"])
    body = slack["parameters"]["jsonBody"]
    # An n8n expression that injects the GENERATE NODE'S summary by name — nodes
    # chain in series, so bare $json.summary would read the previous connector's
    # response for any node after the first (the empty-Gmail bug).
    assert body.startswith("={{")
    assert "$('Generate summary').item.json.summary" in body
    assert "chat.postMessage" in body and "C123" in body
    # No INTERNAL "}}" — n8n closes an expression at the first literal "}}", so a nested
    # object brace collision would truncate it ("invalid syntax"). Only the terminator.
    assert "}}" not in body[3:-3]


def test_oauth_connector_node_passes_the_validator() -> None:
    _, errors = validate_workflow(build_rag_workflow(_slack_spec(message_field="text")))
    assert errors == []


def test_second_connector_still_gets_the_summary() -> None:
    # Nodes chain in SERIES: with two connectors, $json at the second node is the
    # FIRST connector's proxy response — a Docs+Gmail agent live-sent an empty
    # email. Every message expression must reference the Generate node BY NAME.
    spec = replace(
        _BASE,
        integrations=["google-docs", "gmail"],
        connector_proxy_url="http://sila-api:8001/v1/internal/connectors/proxy",
        connectors={
            "google-docs": ConnectorNode(
                provider="google-docs",
                endpoint="documents/create",
                data={},
                token="c1",
                message_field="message",
            ),
            "gmail": ConnectorNode(
                provider="gmail",
                endpoint="users/messages/send",
                data={"to": "a@b.com"},
                token="c2",
                message_field="message",
            ),
        },
    )
    wf = build_rag_workflow(spec)
    for name in ("Google-docs (documents/create)", "Gmail (users/messages/send)"):
        body = next(n for n in wf["nodes"] if n["name"] == name)["parameters"]["jsonBody"]
        assert "$('Generate summary').item.json.summary" in body
        assert "}}" not in body[3:-3]
    # "Email me the file": the Docs node runs FIRST (despite alphabetical order) so
    # the Gmail body can include the doc's link from the Docs node's response.
    names = [n["name"] for n in wf["nodes"]]
    assert names.index("Google-docs (documents/create)") < names.index(
        "Gmail (users/messages/send)"
    )
    gmail_body = next(n for n in wf["nodes"] if "Gmail" in n["name"])["parameters"]["jsonBody"]
    assert "docs.google.com/document/d/" in gmail_body
    assert '$("Google-docs (documents/create)").item.json.documentId' in gmail_body
    # …and forwards the doc id so the proxy attaches the exported PDF.
    assert '"attach_doc_id"' in gmail_body
    assert "}}" not in gmail_body[3:-3]
    # The Docs node never self-references its own (not-yet-existing) response.
    docs_body = next(n for n in wf["nodes"] if "Google-docs" in n["name"])["parameters"]["jsonBody"]
    assert "documentId" not in docs_body
    # No generate node (webhook Q&A) → the trigger payload carries the text.
    bare = replace(spec, run_url=None, run_token=None)
    wf2 = build_rag_workflow(bare)
    body2 = next(n for n in wf2["nodes"] if "Gmail" in n["name"])["parameters"]["jsonBody"]
    assert "$json.summary" in body2


# --- Email destination → Sila SMTP send (no creds in n8n) -------------------


def test_email_destination_emits_sila_send_node() -> None:
    wf = build_rag_workflow(
        replace(
            _BASE,
            integrations=["email"],
            email_url="http://sila-api:8001/v1/internal/agents/send-email",
            email_token="email.sig",
        )
    )
    email = next(n for n in wf["nodes"] if n["name"] == "Email summary")
    assert email["type"] == NODE_HTTP
    assert "credentials" not in email  # n8n holds NO SMTP credential
    p = email["parameters"]
    assert p["url"].endswith("/v1/internal/agents/send-email")
    body = p["jsonBody"]
    assert body.startswith("={{") and "$('Generate summary').item.json.summary" in body
    assert "}}" not in body[3:-3]  # no internal terminator collision (} } spacing)
    hdr = p["headerParameters"]["parameters"][0]
    assert hdr["name"] == "X-Sila-Email-Token"
    assert hdr["value"] == "email.sig"
