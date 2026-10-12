"""Seed the Oman PDPL source document into a workspace Knowledge Base.

Optional, run on demand. Ingesting the PDPL text into a KB lets the LLM
gatekeeper (and Ask-Orca-style retrieval) ground findings in the full article
text, complementing the machine rules in infra/policies/pdpl.yaml.

Run inside the api container:
    docker compose exec sila-api python -m scripts.seed_pdpl_kb \
        <workspace_id> <actor_user_id>

The document at /policies/oman-pdpl-source.md (mounted from infra/policies/) is
uploaded; the worker then parses + embeds it asynchronously.
"""

import asyncio
import io
import sys
import uuid
from pathlib import Path

from fastapi import UploadFile

from sila.db.session import AsyncSessionLocal
from sila.services.kb_service import build_kb_service

SOURCE_PATH = Path("/policies/oman-pdpl-source.md")
KB_NAME = "Oman PDPL"
KB_DESCRIPTION = (
    "Personal Data Protection Law (RD 6/2022) + Executive Regulations, for compliance grounding."
)


async def seed(workspace_id: uuid.UUID, actor_id: uuid.UUID) -> None:
    if not SOURCE_PATH.exists():
        raise SystemExit(f"Source not found: {SOURCE_PATH} (is infra/policies mounted?)")
    content = SOURCE_PATH.read_bytes()

    async with AsyncSessionLocal.begin() as db:
        svc = build_kb_service(db)
        kb = await svc.create_kb(
            workspace_id=workspace_id,
            name=KB_NAME,
            description=KB_DESCRIPTION,
            actor_id=actor_id,
        )
    kb_id = uuid.UUID(kb["id"])
    print(f"created KB {kb_id} ({KB_NAME})")

    async with AsyncSessionLocal.begin() as db:
        svc = build_kb_service(db)
        upload = UploadFile(filename="oman-pdpl.txt", file=io.BytesIO(content))
        result = await svc.upload_document(
            kb_id=kb_id, workspace_id=workspace_id, actor_id=actor_id, file=upload
        )
    print(
        f"uploaded document {result['document_id']} (status={result['status']}) — "
        "the worker will parse + embed it shortly."
    )


if __name__ == "__main__":
    if len(sys.argv) != 3:
        raise SystemExit("usage: python -m scripts.seed_pdpl_kb <workspace_id> <actor_user_id>")
    asyncio.run(seed(uuid.UUID(sys.argv[1]), uuid.UUID(sys.argv[2])))
