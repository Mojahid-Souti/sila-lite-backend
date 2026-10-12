"""When a Sila conversation is an OKR conversation.

Two ways in, and no way out for the rest of the session:

  * the OKR Reporting Pack template is pinned (the WS hello's `template`);
  * a file the user attaches is an OKR sheet — by the pack's own header test
    (`services/okr/detect.py`), whether it rides the message inline or was
    uploaded as a dataset (an .xlsx keeps its original workbook beside the CSV).

What an OKR conversation changes is decided elsewhere — here only the question
"does it carry an OKR sheet" is answered, from the bytes, never the file name.

Complexity: O(size of the attached text) for inline files; one small read of
each uploaded file's head.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

from sila.services.okr_optional import is_okr_csv, is_okr_workbook
from sila.services.sila.orchestrator.runner import ATTACH_RE, DATASET_RE
from sila.services.sila.residency_gateway import REFUSAL, ResidencyGateway

# Enough of an uploaded CSV to find its header (the parser looks at 20 records).
_HEAD_BYTES = 256 * 1024


async def admit_turn(
    gateway: ResidencyGateway, content: str, db: Any, user_id: uuid.UUID
) -> str | None:
    """Before a turn runs: mark the session confidential when this message attaches
    an OKR sheet, and return the refusal to show instead of running it when the
    session is confidential and no local model is available (else None).

    Runs before ANY model call of the turn, so a refusal sends nothing anywhere.
    """
    if not gateway.confidential and await message_carries_okr_sheet(content, db, user_id):
        gateway.mark_confidential("okr sheet attached")
    if gateway.confidential and not await gateway.local_available():
        return REFUSAL
    return None


async def message_carries_okr_sheet(content: str, db: Any, user_id: uuid.UUID) -> bool:
    """Does this user message attach an OKR sheet (inline or as a dataset)?"""
    if any(is_okr_csv(m.group("body")) for m in ATTACH_RE.finditer(content)):
        return True
    for match in DATASET_RE.finditer(content):
        if await _dataset_is_okr(db, match.group("id"), user_id):
            return True
    return False


async def stash_carries_okr_sheet(files: object, db: Any, user_id: uuid.UUID) -> bool:
    """Do a session's stashed attachments (resumed from its row) hold an OKR sheet?"""
    for entry in files if isinstance(files, list) else []:
        if not isinstance(entry, dict):
            continue
        if entry.get("dataset_id"):
            if await _dataset_is_okr(db, str(entry["dataset_id"]), user_id):
                return True
        elif is_okr_csv(str(entry.get("content") or "")):
            return True
    return False


async def _dataset_is_okr(db: Any, dataset_id: str, user_id: uuid.UUID) -> bool:
    from sila.db.repositories.sila import SilaRepository

    try:
        record = await SilaRepository(db).get_dataset(uuid.UUID(dataset_id), user_id)
    except ValueError:
        return False
    if record is None:
        return False
    stored = Path(record.path)
    workbook = stored.with_suffix(".xlsx")
    try:
        if workbook.is_file():
            return is_okr_workbook(workbook.read_bytes())
        with stored.open("rb") as handle:
            head = handle.read(_HEAD_BYTES)
    except OSError:
        return False
    return is_okr_csv(head.decode("utf-8-sig", errors="replace"))
