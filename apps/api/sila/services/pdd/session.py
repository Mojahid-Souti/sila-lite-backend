"""PDD Template — Phases 7+8: the session orchestration SILA's tools drive.

All state is one JSON-safe dict on the chat session (`build_state["pdd"]`),
so it survives reconnects like every other durable key. The tools are thin;
this module owns the sequence: ingest → extract → derive → gap register
(which IS the propose-first gate) → answers/edits → quality report → export.

The quality report (Phase 8) is computed HERE and shown BEFORE export:
coverage %, groundedness % (100 by construction — the dropped ledger shows
what was deleted), open-item count, and the sections at risk.

Complexity: O(source + items) per stage; state round-trips as plain dicts.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from sila.services.pdd import derive, extract, gaps, ingest, pipeline
from sila.services.pdd.gaps import Gap, GapRegister
from sila.services.pdd.ingest import Speaker
from sila.services.pdd.registry import DOC_TYPE_LABEL, flat_sections


def _register_from(state: dict[str, Any]) -> GapRegister:
    return GapRegister(gaps=[Gap(**g) for g in state.get("gaps") or []])


def _store_register(state: dict[str, Any], register: GapRegister) -> None:
    state["gaps"] = [asdict(g) for g in register.gaps]


def _roster_from(state: dict[str, Any]) -> list[Speaker]:
    return [Speaker(**s) for s in state.get("roster") or []]


def start(
    *, doc_type: str, transcript: str, process_name: str = "", department: str = ""
) -> dict[str, Any]:
    """Ingest: normalize, roster (code only), chunk. Returns the session state
    dict plus a relay summary."""
    if doc_type not in DOC_TYPE_LABEL:
        raise ValueError("doc_type must be 'rpa' or 'macro'")
    source = ingest.normalize(transcript)
    if len(source.strip()) < 200:
        raise ValueError("the transcript is too short to document a process (need real content)")
    roster = ingest.build_roster(source)
    state: dict[str, Any] = {
        "doc_type": doc_type,
        "source": source,
        "roster": [asdict(s) for s in roster],
        "meta": {
            "process_name": process_name.strip(),
            "department": department.strip(),
            "version": "0.1",
            "author": roster[0].name if roster else "",
            "date": "",
            "classification": "CONFIDENTIAL",
            "status": "Draft",
        },
        "content": {},
        "gaps": [],
        "dropped": [],
        "extracted_count": 0,
        "undefined_acronyms": [],
        "stage": "ingested",
    }
    return state


def ingest_summary(state: dict[str, Any]) -> dict[str, Any]:
    roster = _roster_from(state)
    return {
        "doc_type_label": DOC_TYPE_LABEL[state["doc_type"]],
        "chars": len(state["source"]),
        "chunks": len(ingest.chunk_source(state["source"])),
        "speakers": [
            {"name": s.name, "role": s.role or "role unknown", "mentions": s.mentions}
            for s in roster
        ],
    }


def _result_from(state: dict[str, Any]) -> extract.ExtractionResult:
    from sila.services.pdd.fields import Evidence

    result = extract.ExtractionResult()
    for item in state.get("items") or []:
        ev = item["evidence"]
        result.items.append(
            extract.ExtractedItem(
                section_id=item["section_id"],
                payload=item["payload"],
                evidence=Evidence(ev["speaker"], ev["quote"], tuple(ev["span"])),
            )
        )
    result.dropped = list(state.get("dropped") or [])
    return result


def _store_result(state: dict[str, Any], result: extract.ExtractionResult) -> None:
    state["items"] = [
        {
            "section_id": i.section_id,
            "payload": i.payload,
            "evidence": {
                "speaker": i.evidence.speaker,
                "quote": i.evidence.quote,
                "span": list(i.evidence.span),
            },
        }
        for i in result.items
    ]
    state["dropped"] = result.dropped
    state["extracted_count"] = len(result.items)


async def extract_next_chunk(
    state: dict[str, Any], llm: Any, progress: Any = None
) -> dict[str, Any]:
    """Process ONE chunk (all five family prompts) per call — extraction is
    resumable, so a 40k transcript never hits the tool timeout. When the last
    chunk lands, derivation + the gap register run and the state moves to the
    question stage. Returns {done, chunk, chunks, …quality when done}."""
    source = str(state["source"])
    roster = _roster_from(state)
    chunks = ingest.chunk_source(source)
    cursor = int(state.get("chunk_cursor") or 0)
    if cursor >= len(chunks):
        return {"done": True, "chunk": len(chunks), "chunks": len(chunks), **quality_report(state)}
    result = _result_from(state)
    chunk = chunks[cursor]
    for fi, family in enumerate(extract.FAMILIES):
        if progress is not None:
            progress(
                f"Reading part {cursor + 1}/{len(chunks)} — "
                f"{family.replace('_', ' ')} ({fi + 1}/{len(extract.FAMILIES)})…"
            )
        try:
            raw = await llm.complete_json(
                system_prompt=extract.family_prompt(family, state["doc_type"]),
                user_prompt=(f"TRANSCRIPT CHUNK (offsets 0..{len(chunk.text)}):\n{chunk.text}"),
                timeout_s=120.0,
            )
        except Exception:  # noqa: BLE001 — one bad call must not sink the chunk
            if progress is not None:
                progress(f"One extraction call failed ({family}) — skipped, moving on.")
            continue
        if isinstance(raw, dict):
            extract.validate_chunk_output(
                raw,
                family=family,
                chunk=chunk,
                source=source,
                roster=roster,
                doc_type=state["doc_type"],
                result=result,
            )
    _store_result(state, result)
    state["chunk_cursor"] = cursor + 1
    if cursor + 1 < len(chunks):
        return {"done": False, "chunk": cursor + 1, "chunks": len(chunks)}
    # Last chunk → derive + gap register (pure code).
    content, undefined = derive.derive(
        result, source=source, roster=roster, doc_type=state["doc_type"]
    )
    register = gaps.build_register(
        content, doc_type=state["doc_type"], undefined_acronyms=undefined, meta=state["meta"]
    )
    state["content"] = content
    state["undefined_acronyms"] = undefined
    state["stage"] = "gaps"
    _store_register(state, register)
    return {"done": True, "chunk": len(chunks), "chunks": len(chunks), **quality_report(state)}


def answer_gap(
    state: dict[str, Any], *, gap_id: str, answer: str = "", waive: bool = False
) -> dict[str, Any]:
    """One turn of the clarifying loop. Answers write back as user_answered
    content; waives move the question to Section 8."""
    register = _register_from(state)
    if gap_id.strip().lower() in ("all", "*"):
        register.waive_all_open()
        _store_register(state, register)
        return quality_report(state)
    if waive:
        gaps.waive(register, gap_id)
    else:
        gap = register.resolve(gap_id)
        if gap.section_id == "front.cover":
            key = "process_name" if "process name" in gap.question else "department"
            if not answer.strip():
                raise ValueError("an empty answer cannot close a gap — waive it instead")
            state["meta"][key] = answer.strip()
            gap.status = "answered"
            gap.answer = answer.strip()
        else:
            gaps.apply_answer(
                register,
                state["content"],
                gap_id=gap_id,
                answer=answer,
                doc_type=state["doc_type"],
            )
    _store_register(state, register)
    return quality_report(state)


def apply_edits(state: dict[str, Any], patches: list[dict[str, Any]]) -> list[str]:
    from sila.services.pdd import refine

    return refine.apply_patches(
        state["content"], state["meta"], patches, doc_type=state["doc_type"]
    )


def quality_report(state: dict[str, Any]) -> dict[str, Any]:
    """Phase 8: the report the user sees BEFORE export. Groundedness of the
    KEPT content is 100% by construction; `dropped` shows what was deleted."""
    register = _register_from(state)
    kept = int(state.get("extracted_count") or 0)
    dropped = len(state.get("dropped") or [])
    at_risk = sorted(
        {g.section_id for g in register.gaps if g.status == "open" and g.section_id[0].isdigit()}
    )
    from collections import Counter

    reason_counts = Counter(str(d.get("reason", "unknown")) for d in state.get("dropped") or [])
    return {
        "coverage_pct": round(gaps.coverage(state.get("content") or {}, state["doc_type"]) * 100),
        "groundedness_pct": 100,
        "extracted_kept": kept,
        "extracted_dropped": dropped,
        "drop_reasons": [{"reason": r, "count": c} for r, c in reason_counts.most_common(3)],
        "open_items": register.open_count,
        "blocking_questions": [
            {
                "id": g.id,
                "section": g.section_id,
                "question": g.question,
                "suggestion": g.suggestion,
            }
            for g in register.blockers
        ],
        "other_questions": [
            {
                "id": g.id,
                "section": g.section_id,
                "question": g.question,
                "suggestion": g.suggestion,
            }
            for g in register.gaps
            if g.status == "open" and not g.blocking
        ][:10],
        "sections_at_risk": at_risk,
    }


def export_bytes(state: dict[str, Any]) -> tuple[bytes, str, dict[str, Any]]:
    """The export boundary: gate → redact → render. Returns (docx bytes,
    filename, quality report). Raises ValueError while blocked."""
    register = _register_from(state)
    pseudo = ingest.Pseudonymizer.from_roster(_roster_from(state))
    doc = pipeline.export_document(
        doc_type=state["doc_type"],
        meta=state["meta"],
        content=state["content"],
        register=register,
        pseudonymizer=pseudo,
    )
    _store_register(state, register)
    name = str(state["meta"].get("process_name") or "process").strip() or "process"
    safe = "".join(c if c.isalnum() or c in " -_" else "" for c in name).strip()
    filename = f"{safe.replace(' ', '-').lower()}-{state['doc_type']}-design.docx"
    return doc, filename, quality_report(state)


def section_overview(state: dict[str, Any]) -> str:
    """A compact per-section fill map for the chat ('1.1 ✓ · 1.2 — open …')."""
    from sila.services.pdd.render import _payload_present

    marks = []
    for section in flat_sections(state["doc_type"]):
        if section.kind in ("prose", "list", "table"):
            filled = _payload_present(section, (state.get("content") or {}).get(section.id))
            marks.append(f"{section.id} {'✓' if filled else '·'}")
    return " ".join(marks)
