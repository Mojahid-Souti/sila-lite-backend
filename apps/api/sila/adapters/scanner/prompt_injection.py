"""Rule-based prompt-injection scanner.

Phase 6 part 1 — pattern library only, no ML. Catches the well-known
attack families:
  - role-override ("you are now…", "ignore previous instructions")
  - system-prompt leak attempts ("print the system prompt")
  - tool-impersonation ("act as if the user is admin")
  - jailbreak markers ("DAN mode", "developer mode")

Each pattern carries a finding_type + severity. Findings are normalized
into the same `Finding` dataclass the PII scanner emits, so the service
treats them identically for the auto-approve gate.

Future drop-in replacement: Rebuff / Prompt-Guard via Ollama, both swap
in via the AgentScanner Protocol (Rule 5). No call-site changes needed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

from .base import AgentScanner, Finding, ScanInput

CONTEXT_WIDTH: Final[int] = 60


@dataclass(frozen=True)
class _Rule:
    finding_type: str
    pattern: re.Pattern[str]
    severity: str

    def redact(self, raw: str) -> str:
        return raw if len(raw) <= 80 else raw[:77] + "..."


# Each rule uses (?i) so the entire regex is case-insensitive.
RULES: tuple[_Rule, ...] = (
    _Rule(
        finding_type="injection.instruction_override",
        pattern=re.compile(
            r"(?i)(ignore|disregard|forget)\s+(all\s+)?(previous|prior|earlier|the\s+above)\s+(instructions?|rules?|prompts?)"
        ),
        severity="critical",
    ),
    _Rule(
        finding_type="injection.role_override",
        pattern=re.compile(r"(?i)you\s+are\s+(now|actually)\s+(?:a|an|the)\s+\w+"),
        severity="high",
    ),
    _Rule(
        finding_type="injection.system_leak",
        pattern=re.compile(
            r"(?i)(print|output|show|reveal|display|repeat)\s+(?:the\s+|your\s+)?(system|initial|hidden|secret)\s+prompt"
        ),
        severity="high",
    ),
    _Rule(
        finding_type="injection.jailbreak_marker",
        pattern=re.compile(r"(?i)\b(DAN\s*mode|developer\s*mode|jailbreak|do\s+anything\s+now)\b"),
        severity="critical",
    ),
    _Rule(
        finding_type="injection.privilege_escalation",
        # Matches "act as admin", "pretend you are root", "behave as if you were sudo", etc.
        # Both `as` and `you are/were` are independently optional.
        pattern=re.compile(
            r"(?i)\b(act|pretend|behave)\s+(?:as\s+(?:if\s+)?)?(?:you\s+(?:are|were)\s+)?(?:an?\s+)?(admin|root|sudo|superuser)"
        ),
        severity="critical",
    ),
    _Rule(
        finding_type="injection.delimiter_attack",
        pattern=re.compile(
            r"(?i)(?:```|\*\*\*|---|\#\#\#)\s*end\s+of\s+(system|prompt|instructions?)"
        ),
        severity="medium",
    ),
    _Rule(
        finding_type="injection.encoded_payload",
        pattern=re.compile(
            r"(?i)(base64|rot13|reverse|decode)\s+(?:the\s+)?(following|below|this)"
        ),
        severity="medium",
    ),
)


class RuleBasedInjectionScanner(AgentScanner):
    name = "prompt_injection"

    def __init__(self, rules: tuple[_Rule, ...] = RULES) -> None:
        self._rules = rules

    async def scan(self, agent_input: ScanInput) -> list[Finding]:
        findings: list[Finding] = []
        for location, text in (
            ("system_prompt", agent_input.system_prompt),
            ("sample_input", agent_input.sample_input),
        ):
            if not text:
                continue
            findings.extend(self._scan_text(location, text))
        return findings

    def _scan_text(self, location: str, text: str) -> list[Finding]:
        out: list[Finding] = []
        for rule in self._rules:
            for m in rule.pattern.finditer(text):
                raw = m.group(0)
                start, end = m.span()
                ctx_start = max(0, start - CONTEXT_WIDTH)
                ctx_end = min(len(text), end + CONTEXT_WIDTH)
                context = text[ctx_start:ctx_end].replace("\n", " ")
                out.append(
                    Finding(
                        finding_type=rule.finding_type,
                        severity=rule.severity,
                        location=location,
                        matched_full=raw,
                        matched_preview=rule.redact(raw),
                        metadata={"context": context},
                    )
                )
        return out
