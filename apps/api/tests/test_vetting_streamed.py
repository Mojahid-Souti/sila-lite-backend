"""Unit tests for VettingService.scan_streamed — the inline, per-scanner path the
SILA graph's vetting node uses to show the security review as live chat steps.

Deterministic + DB-free: fake repos + fake scanners injected onto the service, so
no Postgres / Ollama is touched. Asserts (a) one on_step per scanner, in order,
(b) the same hard-gate outcome as the background path (clean internal →
auto_approved; a critical finding or a Restricted agent → ready_for_review), and
(c) the agent never publishes itself.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from sila.adapters.scanner.base import Finding
from sila.core.constants import AgentStatus
from sila.core.policies.classification_policy import get_classification_policy
from sila.services.vetting_service import VettingService

# Repo-root infra/policies/classification.yaml (deployment sets this via env;
# an isolated unit run must point at the real file).
_POLICY_PATH = Path(__file__).resolve().parents[3] / "infra" / "policies" / "classification.yaml"


class FakeScanner:
    def __init__(self, name: str, findings: list[Finding] | None = None) -> None:
        self.name = name
        self._findings = findings or []

    async def scan(self, _scan_input: Any) -> list[Finding]:  # noqa: ANN401
        return list(self._findings)


class FakeAgents:
    async def update(self, agent: Any, **kw: Any) -> Any:  # noqa: ANN401
        for k, v in kw.items():
            setattr(agent, k, v)
        return agent


class FakeVersions:
    async def get(self, _agent_id: Any, _version: Any) -> Any:  # noqa: ANN401
        return SimpleNamespace(
            dify_dsl={"prompt_template": [{"role": "system", "text": "Be helpful."}]}
        )


class FakeRun:
    def __init__(self, **kw: Any) -> None:
        self.id = uuid.uuid4()
        self.outcome: str | None = None
        for k, v in kw.items():
            setattr(self, k, v)


class FakeVetting:
    def __init__(self) -> None:
        self.findings_added: list[Finding] | None = None
        self.failed: str | None = None

    async def create_run(self, **kw: Any) -> FakeRun:  # noqa: ANN401
        return FakeRun(**kw)

    async def add_findings(self, *, run_id: Any, workspace_id: Any, findings: Any) -> None:  # noqa: ANN401
        self.findings_added = list(findings)

    async def complete_run(self, run: FakeRun, *, outcome: str, finding_summary: Any) -> FakeRun:  # noqa: ANN401
        run.outcome = outcome
        return run

    async def fail_run(self, run: FakeRun, error: str) -> FakeRun:
        self.failed = error
        return run


class FakeAudit:
    async def record(self, **_kw: Any) -> None:  # noqa: ANN401
        return None


def _agent(classification: str = "internal") -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        workspace_id=uuid.uuid4(),
        status=AgentStatus.DRAFT,
        vetting_status="unvetted",
        classification=classification,
        version=1,
    )


def _make(scanners: list[FakeScanner]) -> VettingService:
    svc = VettingService(
        None,  # type: ignore[arg-type]
        scanners=scanners,  # type: ignore[arg-type]
        policy=get_classification_policy(_POLICY_PATH),
    )
    svc._agents = FakeAgents()  # type: ignore[assignment]
    svc._versions = FakeVersions()  # type: ignore[assignment]
    svc._vetting = FakeVetting()  # type: ignore[assignment]
    svc._audit = FakeAudit()  # type: ignore[assignment]
    return svc


@pytest.mark.asyncio
async def test_scan_streamed_clean_internal_auto_approves_and_streams_each_scanner() -> None:
    steps: list[str] = []
    scanners = [
        FakeScanner("pii"),
        FakeScanner("prompt_injection"),
        FakeScanner("llm_review"),
        FakeScanner("workflow_rules"),
    ]
    svc = _make(scanners)
    agent = _agent("internal")

    run = await svc.scan_streamed(
        agent=agent, actor_id=uuid.uuid4(), on_step=lambda name, _f: steps.append(name)
    )

    # One step per scanner, in registry order.
    assert steps == ["pii", "prompt_injection", "llm_review", "workflow_rules"]
    assert run.outcome == "auto_approved"
    assert agent.vetting_status == "approved"
    # Never self-published — the agent stays in the review pipeline.
    assert agent.status == AgentStatus.IN_REVIEW


@pytest.mark.asyncio
async def test_scan_streamed_critical_finding_goes_to_review() -> None:
    critical = Finding(
        finding_type="pii.national_id",
        severity="critical",
        location="system_prompt",
        matched_full="12345678",
        matched_preview="1234****",
        metadata={},
    )
    svc = _make([FakeScanner("pii", [critical]), FakeScanner("llm_review")])
    agent = _agent("internal")

    run = await svc.scan_streamed(agent=agent, actor_id=uuid.uuid4(), on_step=lambda _n, _f: None)

    assert run.outcome == "ready_for_review"
    assert agent.vetting_status == "ready_for_review"
    assert svc._vetting.findings_added  # the finding was persisted


@pytest.mark.asyncio
async def test_scan_streamed_restricted_always_needs_human_even_when_clean() -> None:
    svc = _make([FakeScanner("pii"), FakeScanner("llm_review")])
    agent = _agent("restricted")

    run = await svc.scan_streamed(agent=agent, actor_id=uuid.uuid4(), on_step=lambda _n, _f: None)

    assert run.outcome == "ready_for_review"
    assert agent.vetting_status == "ready_for_review"
