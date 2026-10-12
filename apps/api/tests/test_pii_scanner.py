"""Unit tests for the Omani-PII scanner.

Synthetic data only — none of these patterns are real citizen records.
"""

from __future__ import annotations

import pytest

from sila.adapters.scanner.base import ScanInput
from sila.adapters.scanner.pii import PIIScanner


@pytest.fixture
def scanner() -> PIIScanner:
    return PIIScanner()


@pytest.mark.asyncio
async def test_clean_prompt_returns_no_findings(scanner):
    out = await scanner.scan(ScanInput(system_prompt="You are a helpful assistant."))
    assert out == []


# ---- IBAN -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_detects_oman_iban(scanner):
    out = await scanner.scan(ScanInput(system_prompt="Customer IBAN: OM81CBOM0000001234567890"))
    assert len(out) == 1
    assert out[0].finding_type == "pii.oman_iban"
    assert out[0].severity == "critical"
    assert out[0].matched_full == "OM81CBOM0000001234567890"
    # preview must redact
    assert "***" in out[0].matched_preview or "*" in out[0].matched_preview


@pytest.mark.asyncio
async def test_ignores_invalid_iban_country(scanner):
    out = await scanner.scan(ScanInput(system_prompt="IBAN: DE89370400440532013000"))
    types = [f.finding_type for f in out]
    assert "pii.oman_iban" not in types


# ---- Mobile ---------------------------------------------------------------


@pytest.mark.asyncio
async def test_detects_oman_mobile_with_country_code(scanner):
    out = await scanner.scan(ScanInput(system_prompt="Call me at +968 9123 4567"))
    assert any(f.finding_type == "pii.oman_mobile" for f in out)


@pytest.mark.asyncio
async def test_detects_oman_mobile_without_country_code(scanner):
    out = await scanner.scan(ScanInput(system_prompt="Mobile 91234567 for callback"))
    assert any(f.finding_type == "pii.oman_mobile" for f in out)


@pytest.mark.asyncio
async def test_does_not_match_short_numbers(scanner):
    out = await scanner.scan(ScanInput(system_prompt="The answer is 42 or maybe 1234567."))
    assert all(f.finding_type != "pii.oman_mobile" for f in out)


# ---- National ID ----------------------------------------------------------


@pytest.mark.asyncio
async def test_detects_national_id_with_label(scanner):
    out = await scanner.scan(ScanInput(system_prompt="Civil ID 12345678 belongs to a sample user."))
    assert any(f.finding_type == "pii.oman_national_id" for f in out)


@pytest.mark.asyncio
async def test_ignores_bare_8_digit_without_context(scanner):
    # 8-digit value with no PII label nearby — should not flag
    # (the context-label heuristic suppresses low-confidence matches)
    out = await scanner.scan(ScanInput(system_prompt="Use port 12345678 for the connection."))
    assert all(f.finding_type != "pii.oman_national_id" for f in out)


# ---- Vehicle plate --------------------------------------------------------


@pytest.mark.asyncio
async def test_detects_plate_with_label(scanner):
    out = await scanner.scan(ScanInput(system_prompt="The plate 12345 A belongs to the car"))
    # weak recognizer requires label
    types = [f.finding_type for f in out]
    assert "pii.oman_vehicle_plate" in types


# ---- Multi-location -------------------------------------------------------


@pytest.mark.asyncio
async def test_scans_sample_input_too(scanner):
    out = await scanner.scan(
        ScanInput(
            system_prompt="You help users with banking questions.",
            sample_input="My IBAN is OM81CBOM0000001234567890",
        )
    )
    locations = [f.location for f in out]
    assert "sample_input" in locations


# ---- Empty input ----------------------------------------------------------


@pytest.mark.asyncio
async def test_empty_input_no_error(scanner):
    out = await scanner.scan(ScanInput())
    assert out == []


# ---- Multiple findings in one prompt --------------------------------------


@pytest.mark.asyncio
async def test_detects_multiple_distinct_types(scanner):
    prompt = (
        "Reply: account IBAN OM81CBOM0000001234567890, mobile +968 9123 4567, civil id 12345678."
    )
    out = await scanner.scan(ScanInput(system_prompt=prompt))
    found_types = {f.finding_type for f in out}
    assert "pii.oman_iban" in found_types
    assert "pii.oman_mobile" in found_types
    assert "pii.oman_national_id" in found_types
