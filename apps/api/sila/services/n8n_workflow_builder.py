"""Assemble a RAG n8n workflow definition from a SILA build spec.

Pure, deterministic, no I/O: spec → n8n workflow JSON (nodes + connections).
The chain is linear so the read-only build canvas can lay it out left→right:

    [Receive message] → [Retrieve knowledge] → [Generate answer]
        → (optional integration nodes) → [Respond]

Only allow-listed node types are emitted — NO Code nodes, ever. Retrieval is an
HTTP Request to Sila's OWN KB-search endpoint (not a user-supplied URL), so the
workflow grounds answers in the workspace's pgvector KB.

Complexity: O(n) over the (bounded) node list.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field
from typing import Any

# Allow-listed node types (mirrors workflow_validator's contract; no Code nodes).
# Executable n8n main-flow nodes only — the RAG (KB + LLM) runs in Sila via an
# httpRequest to the internal agent-run endpoint, NOT a langchain sub-node (which
# can't execute on the main flow). typeVersions match n8n 2.x.
NODE_WEBHOOK = "n8n-nodes-base.webhook"
NODE_SCHEDULE = "n8n-nodes-base.scheduleTrigger"
NODE_HTTP = "n8n-nodes-base.httpRequest"
NODE_RESPOND = "n8n-nodes-base.respondToWebhook"
_TYPE_VERSION: dict[str, float] = {
    NODE_WEBHOOK: 2,
    NODE_SCHEDULE: 1.2,
    NODE_HTTP: 4.2,
    NODE_RESPOND: 1.1,
}

# Allow-listed external-effect integration nodes. These have side effects, so the
# spec form/UI must have the user explicitly opt in + confirm before publish.
_INTEGRATION_NODES: dict[str, tuple[str, str]] = {
    "email": (".emailSend", "Send email"),
    "slack": (".slack", "Post to Slack"),
}

_X_START = 240
_X_STEP = 240
_Y = 300


@dataclass(frozen=True)
class ConnectorNode:
    """An OAuth integration (WS-9) that executes via Sila's internal Nango proxy.

    The node holds NO third-party token — n8n calls Sila, Sila calls Nango. The
    `data` is baked at build time (e.g. the chosen Slack channel).
    """

    provider: str
    endpoint: str  # e.g. "chat.postMessage"
    method: str = "POST"
    data: dict[str, Any] = field(default_factory=dict)
    # Per-connection capability token (scoped to this provider). Falls back to the
    # spec-level token if unset.
    token: str | None = None
    # The payload field that receives the generated summary at runtime (e.g. Slack's
    # "text"). When set, the post body is an n8n expression injecting $json.summary.
    message_field: str | None = None


@dataclass(frozen=True)
class WorkflowSpec:
    name: str
    pre_prompt: str
    model: str
    ollama_url: str
    kb_search_url: str | None = None  # set when the agent has a knowledge base
    # The RAG runs in Sila: the workflow calls run_url (capability-authed) and gets a
    # summary. Set for KB-backed agents. summary_query/instruction parameterize it.
    run_url: str | None = None
    run_token: str | None = None
    summary_query: str = "overview key points policies responsibilities"
    summary_instruction: str = "Summarize the content into 3-5 concise, professional bullet points."
    # Email destination — the SMTP send runs in Sila (capability-authed), mirroring the
    # Slack proxy: an httpRequest to email_url with the recipient baked into email_token.
    # Sovereign (platform SMTP), no per-user OAuth, no SMTP creds in n8n.
    email_url: str | None = None
    email_token: str | None = None
    # Cron expression for a scheduled publisher (e.g. daily 09:00). None → webhook trigger.
    schedule_cron: str | None = None
    integrations: list[str] = field(default_factory=list)
    # integration name → (n8n credential type, cred id) for nodes that need auth
    # (the B2 typed-credential path: SMTP/email, raw API keys).
    credentials: dict[str, tuple[str, str]] = field(default_factory=dict)
    # WS-9 — OAuth integrations routed through Sila's internal Nango proxy.
    # integration name → ConnectorNode. When present, the integration becomes an
    # httpRequest to `connector_proxy_url` (no n8n credential) — n8n holds no token.
    connectors: dict[str, ConnectorNode] = field(default_factory=dict)
    connector_proxy_url: str | None = None
    # Per-connection capability token (scoped, not a reusable secret).
    connector_token: str | None = None


def _slug(name: str) -> str:
    base = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "agent"
    return f"{base[:40]}-{uuid.uuid4().hex[:8]}"


def build_rag_workflow(spec: WorkflowSpec) -> dict[str, Any]:
    """Spec → an INACTIVE n8n workflow definition (nodes + connections)."""
    nodes: list[dict[str, Any]] = []
    connections: dict[str, Any] = {}
    x = _X_START

    def add(
        node_type: str,
        node_name: str,
        parameters: dict[str, Any],
        credentials: dict[str, Any] | None = None,
    ) -> str:
        nonlocal x
        node: dict[str, Any] = {
            "name": node_name,
            "type": node_type,
            "typeVersion": _TYPE_VERSION.get(node_type, 1),
            "position": [x, _Y],
            "parameters": parameters,
        }
        if credentials:
            node["credentials"] = credentials  # {n8nType: {id, name}} — a ref, not a secret
        nodes.append(node)
        x += _X_STEP
        return node_name

    def link(source: str, target: str) -> None:
        connections[source] = {"main": [[{"node": target, "type": "main", "index": 0}]]}

    # 1. Trigger — a schedule for a publisher (the "daily 9am" the user asked for),
    #    or a webhook for an on-demand Q&A agent.
    scheduled = bool(spec.schedule_cron)
    if scheduled:
        prev = add(
            NODE_SCHEDULE,
            "Schedule",
            {"rule": {"interval": [{"field": "cronExpression", "expression": spec.schedule_cron}]}},
        )
    else:
        prev = add(
            NODE_WEBHOOK,
            "Receive message",
            {"path": _slug(spec.name), "httpMethod": "POST", "responseMode": "responseNode"},
        )

    # 2. Generate — the RAG (KB retrieval + LLM summary) runs in Sila via a
    #    capability-authed httpRequest. The langchain lmChatOllama node can't execute
    #    on n8n's main flow, so Sila is the brain and n8n is a thin orchestrator. The
    #    node returns {"summary": "..."} which downstream nodes reference as $json.summary.
    if spec.run_url and spec.run_token:
        gen = add(
            NODE_HTTP,
            "Generate summary",
            {
                "method": "POST",
                "url": spec.run_url,
                "sendBody": True,
                "specifyBody": "json",
                "jsonBody": json.dumps(
                    {"query": spec.summary_query, "instruction": spec.summary_instruction}
                ),
                "sendHeaders": True,
                "headerParameters": {
                    "parameters": [{"name": "X-Sila-Run-Token", "value": spec.run_token}]
                },
            },
        )
        link(prev, gen)
        prev = gen

    # The doc is the ARTIFACT: google-docs runs FIRST so message destinations
    # (Gmail/Slack/email) can reference its response and include the doc's link —
    # "email me the file". Alphabetical order had gmail before google-docs, so the
    # doc didn't exist yet when the email node ran.
    ordered = sorted(spec.integrations, key=lambda i: (i != "google-docs", i))
    docs_connector = spec.connectors.get("google-docs")
    doc_node = _connector_label(docs_connector) if docs_connector is not None else None

    for integration in ordered:
        # WS-9 — OAuth connector: an httpRequest to Sila's internal proxy. n8n
        # holds NO third-party token; Sila injects the credential via Nango.
        connector = spec.connectors.get(integration)
        if connector is not None and spec.connector_proxy_url:
            node = add(
                NODE_HTTP,
                _connector_label(connector),
                _connector_params(spec, connector, doc_node=doc_node),
            )
            link(prev, node)
            prev = node
            continue

        # Email → the SMTP send runs in Sila (sovereign), not an n8n emailSend node:
        # an httpRequest to the internal send-email endpoint with the summary injected.
        if integration == "email" and spec.email_url and spec.email_token:
            node = add(NODE_HTTP, "Email summary", _email_params(spec, doc_node=doc_node))
            link(prev, node)
            prev = node
            continue

        entry = _INTEGRATION_NODES.get(integration)
        if entry is None:
            continue  # unknown integration → silently skip (allow-list only)
        suffix, label = entry
        creds: dict[str, Any] | None = None
        wiring = spec.credentials.get(integration)
        if wiring:
            n8n_type, cred_id = wiring
            creds = {n8n_type: {"id": cred_id, "name": f"{label} (SILA)"}}
        node = add(f"n8n-nodes-base{suffix}", label, {}, creds)
        link(prev, node)
        prev = node

    # A webhook trigger needs a Respond node to close the HTTP call; a schedule doesn't.
    if not scheduled:
        respond = add(NODE_RESPOND, "Respond", {})
        link(prev, respond)

    return {
        "name": spec.name,
        "nodes": nodes,
        "connections": connections,
        "settings": {},
        "active": False,
    }


def _connector_label(connector: ConnectorNode) -> str:
    return f"{connector.provider.capitalize()} ({connector.endpoint})"


def _summary_expr(spec: WorkflowSpec) -> str:
    """The n8n expression for the generated summary. Nodes are chained in SERIES,
    so `$json` is the PREVIOUS node's output — the second connector in a chain read
    the first connector's proxy response instead of the summary (a live agent with
    Docs + Gmail sent an empty email). Reference the Generate node BY NAME so every
    downstream node gets the summary regardless of chain position; fall back to
    $json.summary when there is no generate node (webhook Q&A agents carry the
    text in the trigger payload)."""
    if spec.run_url and spec.run_token:
        return "$('Generate summary').item.json.summary"
    return "$json.summary"


def _message_expr(spec: WorkflowSpec, doc_node: str | None, provider: str) -> str:
    """The full message expression: the generated summary, plus — when the chain
    also creates a Google Doc — the doc's link, read from the Docs node's create
    response by NODE NAME ("email me the file"). The Docs node itself never
    self-references. No braces in the expression (the `}}` truncation rule)."""
    base = f"({_summary_expr(spec)} || {json.dumps('(no content)')})"
    if doc_node and provider != "google-docs":
        prefix = json.dumps("\n\nDocument: https://docs.google.com/document/d/")
        return (
            f"({base} + {prefix} + "
            f"$({json.dumps(doc_node)}).item.json.documentId + {json.dumps('/edit')})"
        )
    return base


def _connector_params(
    spec: WorkflowSpec, connector: ConnectorNode, doc_node: str | None = None
) -> dict[str, Any]:
    """An httpRequest node that calls Sila's internal Nango proxy. The capability
    token authenticates the call; no third-party credential is attached.

    When the connector has a `message_field`, the body is an n8n EXPRESSION that
    injects the generated summary (_summary_expr) into that field at runtime — so
    the Slack/email post carries the real generated text, not a static placeholder.
    Otherwise the body is static JSON.
    """
    if connector.message_field:
        static = ", ".join(f"{json.dumps(k)}: {json.dumps(v)}" for k, v in connector.data.items())
        # "Email me the FILE": a mail node downstream of a Docs node also forwards
        # the doc's id — the proxy exports that doc as PDF and attaches it.
        if doc_node and connector.provider in ("gmail", "outlook-mail"):
            attach = (
                f"{json.dumps('attach_doc_id')}: $({json.dumps(doc_node)}).item.json.documentId, "
            )
            static = static + (", " if static else "") + attach.rstrip(", ")
        field = json.dumps(connector.message_field)
        msg = f"{field}: {_message_expr(spec, doc_node, connector.provider)}"
        # SPACES inside every brace are load-bearing: n8n closes an expression at the
        # first literal `}}`, so an un-spaced nested object ("...}}") truncates the
        # expression → "invalid syntax". Spacing keeps every internal close as "} }".
        data_js = "{ " + static + (", " if static else "") + msg + " }"
        payload_js = (
            "{ "
            f"{json.dumps('provider')}: {json.dumps(connector.provider)}, "
            f"{json.dumps('method')}: {json.dumps(connector.method)}, "
            f"{json.dumps('endpoint')}: {json.dumps(connector.endpoint)}, "
            f"{json.dumps('data')}: {data_js}"
            " }"
        )
        json_body = "={{ JSON.stringify(" + payload_js + ") }}"
    else:
        payload = {
            "provider": connector.provider,
            "method": connector.method,
            "endpoint": connector.endpoint,
            "data": dict(connector.data),
        }
        json_body = json.dumps(payload)
    params: dict[str, Any] = {
        "method": "POST",
        "url": spec.connector_proxy_url,
        "sendBody": True,
        "specifyBody": "json",
        "jsonBody": json_body,
    }
    token = connector.token or spec.connector_token
    if token:
        params["sendHeaders"] = True
        params["headerParameters"] = {
            "parameters": [{"name": "X-Sila-Connector-Token", "value": token}]
        }
    return params


def _email_params(spec: WorkflowSpec, doc_node: str | None = None) -> dict[str, Any]:
    """An httpRequest node → Sila's internal send-email endpoint. The recipient is
    baked into the capability token (the body can't widen it); the generated summary is
    injected as the email body via an n8n expression — same `} }` spacing rule as the
    Slack node so the body isn't truncated at a literal `}}`."""
    payload_js = (
        "{ "
        f"{json.dumps('subject')}: {json.dumps('Your scheduled summary')}, "
        f"{json.dumps('body')}: {_message_expr(spec, doc_node, 'email')}"
        " }"
    )
    return {
        "method": "POST",
        "url": spec.email_url,
        "sendBody": True,
        "specifyBody": "json",
        "jsonBody": "={{ JSON.stringify(" + payload_js + ") }}",
        "sendHeaders": True,
        "headerParameters": {
            "parameters": [{"name": "X-Sila-Email-Token", "value": spec.email_token}]
        },
    }
