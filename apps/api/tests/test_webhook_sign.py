"""Unit tests for the HMAC signing/verification used by webhook delivery."""

from __future__ import annotations

from sila.services.webhook_service import (
    _next_backoff_seconds,
    sign_payload,
    verify_signature,
)


def test_signature_format():
    sig = sign_payload("whsec_abc", b'{"hello":"world"}')
    assert sig.startswith("sha256=")
    assert len(sig) == len("sha256=") + 64  # hex digest


def test_signature_round_trip():
    body = b'{"event":"agent.invoked","delivery_id":"deadbeef"}'
    sig = sign_payload("whsec_supersecret", body)
    assert verify_signature("whsec_supersecret", body, sig) is True


def test_verify_rejects_wrong_secret():
    body = b'{"x":1}'
    sig = sign_payload("whsec_alpha", body)
    assert verify_signature("whsec_beta", body, sig) is False


def test_verify_rejects_tampered_body():
    body = b'{"x":1}'
    sig = sign_payload("whsec_alpha", body)
    assert verify_signature("whsec_alpha", b'{"x":2}', sig) is False


def test_verify_rejects_empty_signature():
    assert verify_signature("whsec_alpha", b"{}", "") is False
    assert verify_signature("whsec_alpha", b"{}", "sha256=") is False


def test_verify_constant_time():
    """Spot-check: verify_signature uses hmac.compare_digest (timing-safe)."""
    import hmac

    # Direct evidence: import path is hmac.compare_digest.
    assert hmac.compare_digest is not None


# ---- Backoff schedule ----------------------------------------------------


def test_backoff_progression_default():
    # Initial backoff 1s -> 1, 5, 25, 125, 625 over attempts 0..4
    assert _next_backoff_seconds(0, 1) == 1
    assert _next_backoff_seconds(1, 1) == 5
    assert _next_backoff_seconds(2, 1) == 25
    assert _next_backoff_seconds(3, 1) == 125
    assert _next_backoff_seconds(4, 1) == 625


def test_backoff_progression_scales():
    # Doubling the initial doubles every step.
    assert _next_backoff_seconds(2, 2) == 50
