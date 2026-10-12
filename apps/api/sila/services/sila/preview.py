"""Generate a sample output from a KB — the shared 'generate' half of an agent run.

This is the deterministic "show me before you build it" primitive: retrieve from the
knowledge base and summarize with the local model, WITHOUT creating an agent, a
workflow, or posting anywhere. Reused by the internal agent-run endpoint (which then
posts) and by the SILA build preview (which does NOT post — pure dry-run).

Complexity: O(top_k) retrieval + one local generation call.
"""

from __future__ import annotations

import uuid

import httpx
from sqlalchemy.ext.asyncio import AsyncSession

from sila.core.config import settings
from sila.services.kb_service import build_kb_service

_MAX_CONTEXT_CHARS = 8000  # keep the prompt well within the model's window
_TOP_K = 8  # a little more recall; the char cap still bounds the context
_TIMEOUT_S = 120.0

# Grounding rules wrapped around every generation — the raw "{instruction}\n\nContent"
# prompt hallucinated freely. The TASK goes LAST (recency wins on small models) and
# carries the explicit language directive (summary_instruction_for). The old meta-rule
# "same language as the TASK (Arabic in → Arabic out)" PRIMED qwen-7B into Arabic or
# Chinese even when the task said "Write in English." — live-reproduced; never bring
# back a language rule that names example languages.
_GROUNDING = """You are a document-grounded assistant. Follow these rules EXACTLY:
- Use ONLY the SOURCES below. Never add facts that are not in them.
- If the sources don't contain the answer, say so plainly — do not guess.
- Be concise and specific; quote the key figure or sentence when one settles it.
- Output the content directly in Markdown — no preamble, no closing note.

SOURCES:
{chunks}

TASK: {instruction}"""


async def generate_sample(
    *,
    db: AsyncSession,
    workspace_id: uuid.UUID,
    kb_id: uuid.UUID,
    instruction: str,
    model: str,
    query: str,
) -> str:
    """Retrieve from the KB and generate, grounded. Returns "" if the KB has nothing
    to work from (the caller decides how to present that). Raises httpx errors on a
    generation failure so callers can degrade gracefully."""
    result = await build_kb_service(db).search(kb_id, workspace_id, query, top_k=_TOP_K)
    chunks = "\n\n".join(
        f"[{i + 1}] {r['content']}"
        for i, r in enumerate(result.get("results", []))
        if r.get("content")
    )
    if not chunks:
        return ""
    prompt = _GROUNDING.format(instruction=instruction, chunks=chunks[:_MAX_CONTEXT_CHARS])
    async with httpx.AsyncClient(timeout=_TIMEOUT_S) as client:
        resp = await client.post(
            f"{settings.ollama_url}/api/generate",
            json={
                "model": model,
                "prompt": prompt,
                "stream": False,
                # Low temperature: grounded QA/summaries want fidelity, not flair.
                "options": {"temperature": 0.2},
            },
        )
        resp.raise_for_status()
        return str(resp.json().get("response", "")).strip()
