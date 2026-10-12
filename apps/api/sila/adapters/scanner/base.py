"""Agent safety scanner interface.

Rule 5 — every concrete scanner (Presidio PII, rule-based injection,
NeMo-Guardrails-output, Garak-redteam, etc.) implements this Protocol so
the VettingService can run them all uniformly and so future scanners can
be added without touching the orchestration code.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass
class Finding:
    """One issue detected during a scan.

    `matched_full` is the raw match (workspace-admin-readable only).
    `matched_preview` is a redacted, safe-to-display version. Always set both.
    """

    finding_type: str  # "pii.national_id" | "injection.role_override" | ...
    severity: str  # "info" | "low" | "medium" | "high" | "critical"
    location: str  # "system_prompt" | "sample_input" | "tool_config"
    matched_full: str  # raw match (admin-only)
    matched_preview: str  # redacted preview
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScanInput:
    """Input passed to a scanner. Service builds this from the agent + version.

    `dify_dsl` is the full agent definition — LLM-driven scanners look at
    the whole document so they can catch issues that regex-on-prompt scanners
    can't (e.g. an unsafe `model.provider`, a suspicious `opening_statement`).
    Existing regex scanners ignore this field.
    """

    system_prompt: str = ""
    sample_input: str = ""
    tool_names: list[str] = field(default_factory=list)
    classification: str = "internal"
    dify_dsl: dict[str, Any] | None = None


class AgentScanner(Protocol):
    """Stateless per-call scanner.

    Implementations should be cheap to instantiate; expensive setup
    (model loading) is lazily-initialized at module level.
    """

    name: str

    async def scan(self, agent_input: ScanInput) -> list[Finding]:
        """Return all findings. Empty list = clean.

        Must not raise on bad input — return a `Finding(severity='info')`
        instead. The service catches exceptions and records a 'failed' run.
        """
        ...
