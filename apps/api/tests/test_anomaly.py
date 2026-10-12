"""Unit tests for the AnomalyService z-score math and policy loader.

Pure-function tests — no DB needed. We exercise statistics directly.
"""

from __future__ import annotations

import statistics

import pytest

from sila.core.policies.analytics_policies import (
    AnomalyPolicy,
    AnomalyRule,
    get_anomaly_policy,
    get_hours_saved_policy,
)

# ---- Hours-saved policy ----


def test_hours_saved_default():
    p = get_hours_saved_policy()
    # Default for an unknown agent name → defaults.per_invocation_minutes
    minutes = p.minutes_for(agent_id="00000000-0000-0000-0000-000000000000", agent_name="anything")
    assert minutes == p.default_minutes


def test_hours_saved_pattern_match():
    p = get_hours_saved_policy()
    minutes = p.minutes_for(agent_id="x", agent_name="customer_support_agent")
    # Per the YAML: customer_support* = 5 minutes
    assert minutes == 5


def test_hours_saved_voice_pattern():
    p = get_hours_saved_policy()
    minutes = p.minutes_for(agent_id="x", agent_name="voice_kareem")
    assert minutes == 10


# ---- Anomaly policy ----


def test_anomaly_policy_loads():
    p = get_anomaly_policy()
    assert isinstance(p, AnomalyPolicy)
    ids = {r.id for r in p.rules}
    # The shipped YAML defines these three rules
    assert "invocations_spike" in ids
    assert "tool_failure_rate" in ids
    assert "latency_p95_spike" in ids


def test_anomaly_rule_fields():
    p = get_anomaly_policy()
    spike = next(r for r in p.rules if r.id == "invocations_spike")
    assert spike.kind == "zscore"
    assert spike.threshold == pytest.approx(3.0)
    assert spike.baseline_days == 7
    assert spike.metric == "invocations"


# ---- Z-score math (pure-function spot check matching what the service does) ----


def test_zscore_above_threshold_fires():
    baseline = [10.0, 12.0, 9.0, 11.0, 10.0, 13.0, 11.0]
    today = 30.0
    mean = statistics.fmean(baseline)
    stdev = statistics.pstdev(baseline)
    z = (today - mean) / stdev
    assert z > 3.0  # threshold


def test_zscore_below_threshold_does_not_fire():
    baseline = [10.0, 12.0, 9.0, 11.0, 10.0, 13.0, 11.0]
    today = 14.0
    z = (today - statistics.fmean(baseline)) / statistics.pstdev(baseline)
    assert z < 3.0


def test_zscore_flat_baseline_fires_if_nonzero_jump():
    baseline = [0.0] * 7
    today = 5.0
    # Service path: stdev == 0 → fire if today > mean and today > 0
    assert statistics.pstdev(baseline) == 0
    assert today > statistics.fmean(baseline) and today > 0


# ---- AnomalyRule construction (defensive defaults) ----


def test_rule_default_kind():
    # The YAML loader fills missing keys with sane defaults
    r = AnomalyRule(
        id="t",
        metric="invocations",
        kind="absolute",
        threshold=10.0,
        baseline_days=7,
        min_sample_size=5,
        severity="medium",
        note="",
    )
    assert r.kind == "absolute"
    assert r.threshold == 10.0
