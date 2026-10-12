"""WorkflowScanner — PDPL/sovereignty rules over n8n workflow definitions.

Runs only when the agent definition is a workflow (has a `nodes` list); chat
DSLs return no findings so the scanner is a no-op for Dify agents. Every
finding carries `metadata.pdpl_article` so reviewers and the compliance
report can cite the exact Omani regulation.

Severity semantics (classification.yaml):
  critical → hard block (sovereignty: cloud AI nodes; embedded secrets)
  high     → blocks auto-approve, mandatory Reviewer (external sends, Art. 5 data)
  medium   → recorded for review (external HTTP destinations)

Complexity: O(n × r) — n nodes (< 100), r rules (constant, small).
"""

from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlparse

from sila.adapters.scanner.base import AgentScanner, Finding, ScanInput
from sila.core.policies.pdpl_policy import PdplPolicy, get_pdpl_policy

_URL_RE = re.compile(r"https?://[^\s\"'\\]+")


def _preview(raw: str) -> str:
    return raw if len(raw) <= 80 else raw[:77] + "..."


class WorkflowScanner(AgentScanner):
    """Rule-based PDPL checks for workflow agents (engine-neutral findings)."""

    name = "workflow_rules"

    async def scan(self, agent_input: ScanInput) -> list[Finding]:
        dsl = agent_input.dify_dsl
        if not isinstance(dsl, dict) or not isinstance(dsl.get("nodes"), list):
            return []  # not a workflow definition — nothing to scan
        policy = get_pdpl_policy()
        findings: list[Finding] = []
        for node in dsl["nodes"]:
            if isinstance(node, dict):
                findings.extend(self._scan_node(node, policy))
        return findings

    def _scan_node(self, node: dict[str, Any], policy: PdplPolicy) -> list[Finding]:
        findings: list[Finding] = []
        node_type = str(node.get("type", "")).lower()
        node_name = str(node.get("name", "?"))
        location = f"workflow.node:{node_name}"
        params_json = json.dumps(node.get("parameters") or {}, ensure_ascii=False)

        # 1. Sovereignty: cloud AI / external-processing nodes (Art. 37-40)
        denied = next((s for s in policy.denied_node_type_substrings if s in node_type), None)
        if denied:
            findings.append(
                Finding(
                    finding_type="workflow.cloud_node",
                    severity="critical",
                    location=location,
                    matched_full=str(node.get("type", "")),
                    matched_preview=_preview(
                        f"Cloud AI node '{node.get('type', '')}' — data must stay in Oman"
                    ),
                    metadata={"pdpl_article": policy.article("cross_border"), "rule": denied},
                )
            )

        # 2. External-effect sends (Art. 22 — consent + opt-out → human review)
        effect = next(
            (s for s in policy.external_effect_node_type_substrings if s in node_type), None
        )
        if effect:
            findings.append(
                Finding(
                    finding_type="workflow.external_effect",
                    severity="high",
                    location=location,
                    matched_full=str(node.get("type", "")),
                    matched_preview=_preview(
                        f"External send node '{node.get('type', '')}' — marketing/social "
                        "messages need consent + opt-out"
                    ),
                    metadata={"pdpl_article": policy.article("marketing"), "rule": effect},
                )
            )

        # 3. External HTTP destinations (Art. 37-40 cross-border risk)
        for url in _URL_RE.findall(params_json):
            host = urlparse(url).hostname or ""
            if host and not policy.is_internal_host(host):
                findings.append(
                    Finding(
                        finding_type="workflow.external_destination",
                        severity="medium",
                        location=location,
                        matched_full=url,
                        matched_preview=_preview(f"External destination: {host}"),
                        metadata={"pdpl_article": policy.article("cross_border"), "host": host},
                    )
                )

        # 4. Embedded secrets in parameters (Art. 26 security controls)
        for pattern in policy.secret_patterns:
            match = pattern.search(params_json)
            if match:
                findings.append(
                    Finding(
                        finding_type="workflow.embedded_secret",
                        severity="critical",
                        location=location,
                        matched_full=match.group(0),
                        matched_preview="Credential embedded in node parameters [REDACTED]",
                        metadata={"pdpl_article": policy.article("security_controls")},
                    )
                )
                break  # one secret finding per node is enough

        # 5. Art. 5 sensitive-data categories (Ministry permit required)
        params_lower = params_json.lower()
        keyword = next((k for k in policy.sensitive_data_keywords if k in params_lower), None)
        if keyword:
            findings.append(
                Finding(
                    finding_type="workflow.sensitive_data",
                    severity="high",
                    location=location,
                    matched_full=keyword,
                    matched_preview=_preview(
                        f"Sensitive data category '{keyword}' — a Ministry permit is required"
                    ),
                    metadata={"pdpl_article": policy.article("sensitive_data")},
                )
            )

        return findings
