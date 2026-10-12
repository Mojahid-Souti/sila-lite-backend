"""Phase 15 Surface 3B — WorkflowScanner PDPL rules.

Each rule group gets a positive + the clean-workflow negative; chat DSLs are
skipped entirely (the scanner only understands workflow shapes).
"""

import pytest

from sila.adapters.scanner.base import ScanInput
from sila.adapters.scanner.workflow import WorkflowScanner
from sila.core.policies.pdpl_policy import get_pdpl_policy


def _input(definition: dict) -> ScanInput:  # type: ignore[type-arg]
    return ScanInput(dify_dsl=definition)


def _wf(*nodes: dict) -> dict:  # type: ignore[type-arg]
    return {"name": "wf", "nodes": list(nodes)}


CLEAN_WF = _wf(
    {"type": "n8n-nodes-base.webhook", "name": "Webhook", "parameters": {"path": "x"}},
    {
        "type": "@n8n/n8n-nodes-langchain.lmChatOllama",
        "name": "Local LLM",
        "parameters": {"baseUrl": "http://ollama:11434", "model": "qwen2.5:7b-instruct"},
    },
    {"type": "n8n-nodes-base.respondToWebhook", "name": "Respond", "parameters": {}},
)


@pytest.mark.asyncio
async def test_clean_local_workflow_no_findings() -> None:
    findings = await WorkflowScanner().scan(_input(CLEAN_WF))
    assert findings == []


@pytest.mark.asyncio
async def test_chat_dsl_skipped() -> None:
    chat_dsl = {"app": {"name": "chat"}, "model_config": {"provider": "openai"}}
    findings = await WorkflowScanner().scan(_input(chat_dsl))
    assert findings == []


@pytest.mark.asyncio
async def test_cloud_llm_node_is_critical_with_article() -> None:
    wf = _wf({"type": "@n8n/n8n-nodes-langchain.lmChatOpenAi", "name": "GPT", "parameters": {}})
    findings = await WorkflowScanner().scan(_input(wf))
    assert len(findings) == 1
    f = findings[0]
    assert f.finding_type == "workflow.cloud_node"
    assert f.severity == "critical"
    assert f.metadata["pdpl_article"] == get_pdpl_policy().article("cross_border")


@pytest.mark.asyncio
async def test_social_send_node_is_high_art22() -> None:
    wf = _wf({"type": "n8n-nodes-base.twitter", "name": "Post to X", "parameters": {}})
    findings = await WorkflowScanner().scan(_input(wf))
    assert [f.finding_type for f in findings] == ["workflow.external_effect"]
    assert findings[0].severity == "high"
    assert findings[0].metadata["pdpl_article"] == "22"


@pytest.mark.asyncio
async def test_external_http_destination_is_medium() -> None:
    wf = _wf(
        {
            "type": "n8n-nodes-base.httpRequest",
            "name": "Fetch",
            "parameters": {"url": "https://api.example.com/v1/data"},
        }
    )
    findings = await WorkflowScanner().scan(_input(wf))
    assert [f.finding_type for f in findings] == ["workflow.external_destination"]
    assert findings[0].severity == "medium"
    assert findings[0].metadata["host"] == "api.example.com"


@pytest.mark.asyncio
async def test_internal_http_destination_not_flagged() -> None:
    wf = _wf(
        {
            "type": "n8n-nodes-base.httpRequest",
            "name": "Local call",
            "parameters": {"url": "http://sila-api:8001/healthz"},
        }
    )
    findings = await WorkflowScanner().scan(_input(wf))
    assert findings == []


@pytest.mark.asyncio
async def test_embedded_secret_is_critical_and_redacted() -> None:
    wf = _wf(
        {
            "type": "n8n-nodes-base.httpRequest",
            "name": "Leaky",
            "parameters": {
                "url": "http://ollama:11434",
                # Obvious low-entropy placeholder (not a real secret) that still
                # matches the scanner's api_key pattern.
                "headerValue": "api_key: FAKE_PLACEHOLDER_TOKEN_0000",  # gitleaks:allow
            },
        }
    )
    findings = await WorkflowScanner().scan(_input(wf))
    types = [f.finding_type for f in findings]
    assert "workflow.embedded_secret" in types
    secret = next(f for f in findings if f.finding_type == "workflow.embedded_secret")
    assert secret.severity == "critical"
    assert "REDACTED" in secret.matched_preview
    assert "PLACEHOLDER" not in secret.matched_preview


@pytest.mark.asyncio
async def test_sensitive_category_keyword_is_high_art5() -> None:
    wf = _wf(
        {
            "type": "n8n-nodes-base.set",
            "name": "Build payload",
            "parameters": {"value": "patient data and health record export"},
        }
    )
    findings = await WorkflowScanner().scan(_input(wf))
    assert [f.finding_type for f in findings] == ["workflow.sensitive_data"]
    assert findings[0].severity == "high"
    assert findings[0].metadata["pdpl_article"] == "5"


def test_pdpl_policy_loads_all_rule_groups() -> None:
    policy = get_pdpl_policy()
    assert policy.article("sensitive_data") == "5"
    assert policy.article("marketing") == "22"
    assert "openai" in policy.denied_node_type_substrings
    assert "twitter" in policy.external_effect_node_type_substrings
    assert policy.secret_patterns
    assert policy.is_internal_host("sila-api")
    assert not policy.is_internal_host("api.example.com")
