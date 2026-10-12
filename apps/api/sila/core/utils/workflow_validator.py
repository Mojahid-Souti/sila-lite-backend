"""Validation for n8n workflow definitions registered as workflow agents.

Mirrors yaml_validator's contract: validate → (definition, errors). A workflow
agent is invoked via its production webhook, so the definition MUST contain a
Webhook trigger node; we extract its path here.

Complexity: O(n) over nodes (n < 100 in practice).
"""

from typing import Any

# 1 MiB cap, same budget as YAML imports (Phase 2 security review).
WORKFLOW_MAX_BYTES = 1 * 1024 * 1024

_WEBHOOK_NODE_TYPE = "n8n-nodes-base.webhook"
_SCHEDULE_NODE_TYPE = "n8n-nodes-base.scheduleTrigger"
_RESPOND_NODE_TYPE = "n8n-nodes-base.respondToWebhook"


def has_trigger(definition: dict[str, Any]) -> bool:
    """A runnable agent needs a trigger: a Webhook (on-demand Q&A) OR a Schedule
    (a publisher that posts on its own). Either satisfies registration."""
    types = {n.get("type") for n in definition.get("nodes", [])}
    return _WEBHOOK_NODE_TYPE in types or _SCHEDULE_NODE_TYPE in types


def find_webhook_path(definition: dict[str, Any]) -> str | None:
    """Return the first Webhook trigger node's path, or None if absent."""
    for node in definition.get("nodes", []):
        if node.get("type") == _WEBHOOK_NODE_TYPE:
            path = (node.get("parameters") or {}).get("path")
            if isinstance(path, str) and path.strip():
                return path.strip().lstrip("/")
            # n8n falls back to the node's webhookId when no custom path is set.
            webhook_id = node.get("webhookId")
            if isinstance(webhook_id, str) and webhook_id:
                return webhook_id
    return None


def has_respond_node(definition: dict[str, Any]) -> bool:
    """True when the workflow can return real output to the webhook caller."""
    return any(n.get("type") == _RESPOND_NODE_TYPE for n in definition.get("nodes", []))


def validate_workflow(definition: object) -> tuple[dict[str, Any], list[str]]:
    """Validate a workflow definition for registration as an agent.

    Returns (definition, errors). Empty errors = valid. Never raises on bad
    shapes — every problem becomes a human-readable error string. A missing
    'Respond to Webhook' node is NOT an error (n8n acks with a default body);
    the publish UI surfaces it as a tip via has_respond_node().
    """
    errors: list[str] = []
    if not isinstance(definition, dict):
        return {}, ["Workflow definition must be a JSON object"]

    nodes = definition.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        errors.append("Workflow has no nodes")
        return definition, errors

    if not has_trigger(definition):
        errors.append(
            "Workflow needs a trigger to run as an agent — a Webhook (for an on-demand "
            "agent, plus a 'Respond to Webhook' node) or a Schedule (for one that posts "
            "on its own), then publish again"
        )

    return definition, errors
