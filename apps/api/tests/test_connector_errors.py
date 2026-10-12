"""Connector failure diagnostics.

Every provider 4xx used to collapse to "Nango returned 403", which is what made
a missing Teams scope and an unresolvable email recipient cost hours each: Graph
names both faults precisely in the body, and we discarded it. These pin what is
surfaced and — just as important — what is withheld.
"""

import pytest

from sila.adapters.connectors.nango_client import _error_text, _provider_error

# Real bodies, captured from Graph against the live connection.
_NO_RECIPIENTS = (
    b'{"error":{"code":"ErrorInvalidRecipients","message":"At least one recipient is not '
    b"valid., A message can't be sent because it contains no recipients.\"}}"
)
_UNRESOLVED = (
    b'{"error":{"code":"ErrorInvalidRecipients","message":"At least one recipient is not '
    b"valid., Recipient 'Ada Lovelace' is not resolved. All recipients must be resolved "
    b'before a message can be submitted."}}'
)
_MISSING_SCOPE = (
    b'{"error":{"code":"Forbidden","message":"Missing scope permissions on the request. '
    b"API requires one of 'ChannelSettings.Read.All, Channel.ReadBasic.All'. Scopes on "
    b"the request 'ChannelMessage.Read.All, ChannelMessage.Send'.\"}}"
)


def test_permission_failure_names_the_missing_scope() -> None:
    """The one case where withholding the message costs hours. A scope name is
    API metadata, never the user's content, so it is safe to carry."""
    text = _error_text(403, _MISSING_SCOPE)
    assert "Forbidden" in text
    assert "Channel.ReadBasic.All" in text


def test_recipient_failure_carries_the_code_but_not_the_address() -> None:
    """The code is an enum and always safe. The message is not: Graph echoes the
    recipient back, and that is the user's data — it must not travel."""
    text = _error_text(400, _UNRESOLVED)
    assert "ErrorInvalidRecipients" in text
    assert "Ada Lovelace" not in text
    assert "not resolved" not in text


def test_empty_recipient_body_still_yields_its_code() -> None:
    assert "ErrorInvalidRecipients" in _error_text(400, _NO_RECIPIENTS)


@pytest.mark.parametrize(
    "body",
    [b"", b"not json at all", b"[1,2,3]", b'{"ok":false}', b"<html>502</html>"],
)
def test_unrecognisable_bodies_fall_back_to_the_status_alone(body: bytes) -> None:
    """A body we cannot parse must not break the error path — it degrades to
    exactly the message this code produced before."""
    assert _error_text(502, body) == "Nango returned 502"


def test_slack_style_string_error_is_carried() -> None:
    """Slack answers {"ok": false, "error": "channel_not_found"} — a bare string
    where Graph nests an object."""
    assert "channel_not_found" in _error_text(200, b'{"ok":false,"error":"channel_not_found"}')


def test_provider_error_returns_empty_for_a_non_error_body() -> None:
    assert _provider_error(b'{"value":[]}') == ("", "")


@pytest.mark.parametrize(
    "recipient",
    ["Ada Lovelace", "ada", "ada@", "@company.com", "ada@company", "a b@company.com"],
)
def test_draft_rejects_anything_that_is_not_an_address(recipient: str) -> None:
    """The reported symptom: the draft appeared, Send was pressed, and the message
    died. Graph only rejects an unresolvable recipient at SEND time, so the check
    has to happen while the model can still ask for the address."""
    from sila.services.sila.tool_registry import _EMAIL_RE

    assert _EMAIL_RE.match(recipient) is None


@pytest.mark.parametrize(
    "recipient",
    [
        "admin@sila-ai.org",
        "someone@example.com",
        "first.last+tag@sub.example.co.uk",
    ],
)
def test_draft_accepts_real_addresses(recipient: str) -> None:
    from sila.services.sila.tool_registry import _EMAIL_RE

    assert _EMAIL_RE.match(recipient) is not None
