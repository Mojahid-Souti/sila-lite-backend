"""Canonical per-invocation cost telemetry.

ONE shape, produced by every engine (Ollama/LangGraph, Dify, n8n), persisted in
`audit_log.metadata`, and consumed by the cost model. Built for four defects the
previous single blended `tokens` field could not support:

  G2  input and output tokens are recorded SEPARATELY (cloud vendors price them
      differently, and prefill/decode have different local cost profiles);
      reasoning ("thinking") tokens are counted separately again — they are
      billed as output but never shown to the user.
  G3  `agent_id` travels with the row so cost can be grouped per agent, not just
      per workspace (Bazaar revenue share needs per-agent cost).
  G4  `active_seconds` is prefill + decode — the time the GPU is ACTUALLY busy.
      Wall-clock `latency_ms` is kept as a SEPARATE field: it is the right
      measure for user-facing responsiveness and the wrong one for GPU cost.
      Measured on this rig, a cold request was GPU-busy only 43% of its wall
      clock; two of five nvidia-smi samples inside one request read 0% / 14 W.

Ollama returns every field we need on the final stream frame and the previous
code discarded all four durations:

  prompt_eval_count / prompt_eval_duration   -> input tokens / prefill (ns)
  eval_count        / eval_duration          -> output tokens / decode  (ns)
  total_duration, load_duration              -> whole call / weight load (ns)

Complexity: O(1) per invocation — pure dict arithmetic, no I/O.
"""

from __future__ import annotations

from typing import Any

# Metadata schema version. Rows written before this existed have no `v` key and
# carry only the old blended `tokens` — the cost query treats them separately so
# a derivation change never silently rewrites history.
COST_SCHEMA_VERSION = 2

_NS = 1e9


def _f(raw: Any) -> float:
    """Tolerant float — a provider that omits a field must not break the row."""
    try:
        return float(raw or 0)
    except (TypeError, ValueError):
        return 0.0


def _i(raw: Any) -> int:
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def usage_from_ollama(frame: dict[str, Any]) -> dict[str, Any]:
    """Ollama's final /api/chat|generate frame -> the canonical usage block.

    Durations arrive in NANOSECONDS and are converted to seconds here so no
    caller has to remember the unit.
    """
    prefill_s = _f(frame.get("prompt_eval_duration")) / _NS
    decode_s = _f(frame.get("eval_duration")) / _NS
    return {
        "input_tokens": _i(frame.get("prompt_eval_count")),
        "output_tokens": _i(frame.get("eval_count")),
        # Ollama reports thinking tokens inside eval_count; models that expose
        # them separately (Qwen3 /think) set this via `reasoning_tokens`.
        "reasoning_tokens": _i(frame.get("thinking_eval_count")),
        "prefill_seconds": round(prefill_s, 6),
        "decode_seconds": round(decode_s, 6),
        # G4: GPU-busy time, NOT wall clock.
        "active_seconds": round(prefill_s + decode_s, 6),
        "load_seconds": round(_f(frame.get("load_duration")) / _NS, 6),
        "total_seconds": round(_f(frame.get("total_duration")) / _NS, 6),
        "engine": "ollama",
        "model": str(frame.get("model") or ""),
    }


def usage_from_dify(dify_metadata: dict[str, Any]) -> dict[str, Any]:
    """Dify's `metadata.usage` block -> the canonical usage block.

    Dify reports prompt/completion tokens and a wall-clock latency, but no
    prefill/decode split (it does not pass Ollama's durations through). Those
    stay 0 and `active_seconds` is left 0 rather than guessed — the cost query
    must be able to tell "no GPU timing available" from "zero GPU time".
    """
    usage = (dify_metadata or {}).get("usage", {}) or {}
    latency_s = _f(usage.get("latency"))
    return {
        "input_tokens": _i(usage.get("prompt_tokens")),
        "output_tokens": _i(usage.get("completion_tokens")),
        "reasoning_tokens": 0,
        "prefill_seconds": 0.0,
        "decode_seconds": 0.0,
        "active_seconds": 0.0,  # unknown — see docstring
        "load_seconds": 0.0,
        "total_seconds": round(latency_s, 6),
        "engine": "dify",
        "model": str(usage.get("model") or ""),
    }


def cost_metadata(
    usage: dict[str, Any] | None,
    *,
    agent_id: Any = None,
    latency_ms: float | None = None,
    engine: str | None = None,
    surface: str | None = None,
    llm_turns: int | None = None,
    tool_calls: list[tuple[str, float]] | None = None,
    first_turn_input_tokens: int | None = None,
) -> dict[str, Any] | None:
    """Assemble the audit-row metadata for one invocation.

    `agent_id` (G3) and the wall-clock `latency_ms` (G4, kept distinct from
    `active_seconds`) are attached here. `surface` records HOW it was invoked
    (sandbox / public API / concierge) so sandbox and production volume can be
    separated even though both are counted.

    TOOL-DRIVEN TOKEN GROWTH (the connector's own cost). A tool result re-enters
    the prompt, so the NEXT LLM turn pays for it: measured 2,050 -> 4,100 input
    tokens (2.0x) for one tool call. That growth is caused by the connector, not
    the agent's own prompt, and the Bazaar revenue share needs it attributable.
    We therefore record:
      llm_turns              how many LLM round trips the invocation took
      tool_calls             [(tool_name, seconds)] — non-GPU wall per connector
      tool_seconds           their sum
      first_turn_input_tokens  the baseline prompt before any tool result
      tool_input_token_growth  input_tokens - first_turn_input_tokens
      tool_token_multiplier    input_tokens / first_turn_input_tokens

    Returns None when there is nothing worth recording.
    """
    if not usage and latency_ms is None:
        return None
    u = dict(usage or {})
    total_tokens = _i(u.get("input_tokens")) + _i(u.get("output_tokens"))
    meta: dict[str, Any] = {
        "v": COST_SCHEMA_VERSION,
        **u,
        # Blended total kept for continuity with v1 rows + existing dashboards.
        "tokens": total_tokens,
    }
    if engine:
        meta["engine"] = engine
    if surface:
        meta["surface"] = surface
    if agent_id is not None:
        meta["agent_id"] = str(agent_id)
    if latency_ms is not None:
        meta["latency_ms"] = round(float(latency_ms), 1)
    if llm_turns is not None:
        meta["llm_turns"] = int(llm_turns)
    if tool_calls:
        meta["tool_calls"] = [
            {"tool": str(n), "seconds": round(float(s), 3)} for n, s in tool_calls
        ]
        meta["tool_seconds"] = round(sum(float(s) for _, s in tool_calls), 3)
        meta["tool_call_count"] = len(tool_calls)
    if first_turn_input_tokens:
        base = _i(first_turn_input_tokens)
        meta["first_turn_input_tokens"] = base
        total_in = _i(u.get("input_tokens"))
        if base > 0 and total_in >= base:
            meta["tool_input_token_growth"] = total_in - base
            meta["tool_token_multiplier"] = round(total_in / base, 3)
    return meta
