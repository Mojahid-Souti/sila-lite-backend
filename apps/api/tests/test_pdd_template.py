"""PDD Template #2 — Phase 0: registry, field model, renderer, exemplars.

Spec done-when: RPA and Macro both render a complete, professionally styled
Word document with IDENTICAL section numbering, a working ToC, and no bare
headings. Plus the field-model hard rule: `extracted` without evidence is
deleted, never kept.
"""

from __future__ import annotations

import re
import xml.dom.minidom
import zipfile
from io import BytesIO

import pytest

from sila.services.pdd import fields, registry, render
from sila.services.pdd.exemplar import exemplar_content, exemplar_meta


def _document_xml(doc: bytes) -> str:
    with zipfile.ZipFile(BytesIO(doc)) as z:
        return z.read("word/document.xml").decode()


def _render(doc_type: str) -> bytes:
    return render.render_document(doc_type, exemplar_meta(doc_type), exemplar_content(doc_type))


# ---------------------------------------------------------------------------
# Registry — one spine, identical numbering, macro APPENDS and never renumbers
# ---------------------------------------------------------------------------


def test_numbering_identical_macro_only_appends() -> None:
    rpa_ids = [s.id for s in registry.flat_sections("rpa")]
    macro_ids = [s.id for s in registry.flat_sections("macro")]
    assert set(macro_ids) - set(rpa_ids) == {"3.8", "4.4"}
    # Removing the appended ids yields the EXACT same sequence — no renumbering.
    assert [i for i in macro_ids if i not in ("3.8", "4.4")] == rpa_ids
    # And the appends sit where the spec puts them.
    assert macro_ids[macro_ids.index("3.7") + 1] == "3.8"
    assert macro_ids[macro_ids.index("4.3") + 1] == "4.4"


def test_registry_shapes() -> None:
    flat = {s.id: s for s in registry.flat_sections("rpa")}
    assert flat["2.3"].kind == "table" and "Step name" in flat["2.3"].columns
    assert flat["2.3"].required_columns == ("Step name", "Actor")
    assert flat["2.2"].kind == "image" and flat["2.2"].source == "2.3"
    assert registry.NFR_TYPES["rpa"] != registry.NFR_TYPES["macro"]
    with pytest.raises(ValueError, match="unknown document type"):
        registry.sections_for("word")


# ---------------------------------------------------------------------------
# Field model (A.11) — extracted without grounding is DELETED
# ---------------------------------------------------------------------------


def test_extracted_without_evidence_is_deleted() -> None:
    source = "Sarah: we pull three reports and check them line by line."
    grounded = fields.Field(
        value="three reports",
        status="extracted",
        evidence=fields.Evidence("Sarah", "we pull three reports", (7, 28)),
    )
    hallucinated = fields.Field(value="approved by the CFO", status="extracted", evidence=None)
    wrong_quote = fields.Field(
        value="x",
        status="extracted",
        evidence=fields.Evidence("Sarah", "the CFO signs every invoice", (0, 20)),
    )
    derived = fields.Field(value="FR-01", status="derived")
    result = fields.validate_fields([grounded, hallucinated, wrong_quote, derived], source)
    assert grounded in result.kept and derived in result.kept
    assert hallucinated in result.dropped and wrong_quote in result.dropped
    assert result.groundedness == pytest.approx(1 / 3)


def test_field_from_dict_rejects_bad_shapes() -> None:
    with pytest.raises(ValueError, match="status"):
        fields.field_from_dict({"value": "x", "status": "guessed"})
    with pytest.raises(ValueError, match="confidence"):
        fields.field_from_dict({"value": "x", "confidence": "certain"})
    f = fields.field_from_dict(
        {"value": "x", "evidence": {"speaker": "A", "quote": "q", "span": [3, 9]}}
    )
    assert f.evidence is not None and f.evidence.span == (3, 9)


# ---------------------------------------------------------------------------
# Done-when — both doc types render styled, complete, no bare headings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("doc_type", ["rpa", "macro"])
def test_exemplar_renders_complete_document(doc_type: str) -> None:
    doc = _render(doc_type)
    xml_text = _document_xml(doc)
    xml.dom.minidom.parseString(xml_text)  # well-formed
    with zipfile.ZipFile(BytesIO(doc)) as z:
        xml.dom.minidom.parseString(z.read("word/styles.xml").decode())
        footer = z.read("word/footer1.xml").decode()
        settings = z.read("word/settings.xml").decode()
    assert "CONFIDENTIAL" in footer and "PAGE" in footer
    assert "updateFields" in settings  # the ToC refreshes on open
    assert "TOC \\o" in xml_text  # the ToC field is present
    # Every numbered body heading from the registry appears, verbatim.
    for section in registry.flat_sections(doc_type):
        assert f"{section.id}. {section.title}" in xml_text.replace("&amp;", "&")


def test_macro_sections_absent_from_rpa() -> None:
    rpa_xml = _document_xml(_render("rpa"))
    macro_xml = _document_xml(_render("macro"))
    assert "3.8. Macro Technical Build" not in rpa_xml
    assert "3.8. Macro Technical Build" in macro_xml
    assert "4.4. Macro Security &amp; Deployment" in macro_xml


_HEADING_RE = re.compile(r'<w:pStyle w:val="(Heading[123]|Title|Subtitle)"/>')


def test_no_bare_headings_and_no_empty_tables() -> None:
    for doc_type in ("rpa", "macro"):
        xml_text = _document_xml(_render(doc_type))
        # Split into block-level elements; a heading must be followed by
        # CONTENT (a non-heading paragraph or a table) before the next heading.
        blocks = re.findall(r"<w:tbl>.*?</w:tbl>|<w:p>.*?</w:p>|<w:p/>", xml_text)
        kinds = [
            "heading"
            if _HEADING_RE.search(b)
            else "table"
            if b.startswith("<w:tbl>")
            else "content"
            for b in blocks
        ]
        for i, kind in enumerate(kinds):
            if kind != "heading":
                continue
            following = kinds[i + 1 :]
            assert following, f"{doc_type}: trailing bare heading"
            # Container headings may be followed by a subsection heading; a
            # LEAF heading must hit content/table before the document ends.
            assert any(k != "heading" for k in following), f"{doc_type}: bare heading at #{i}"
        # Every table has a header row AND at least one data row.
        for tbl in re.findall(r"<w:tbl>.*?</w:tbl>", xml_text):
            assert tbl.count("<w:tr>") >= 2, f"{doc_type}: table with no data rows"


def test_pseudonyms_consistent_across_stakeholders_raci_signoff() -> None:
    xml_text = _document_xml(_render("rpa"))
    # Rule 5: the same pseudonym threads the stakeholder table, the RACI
    # matrix, and the sign-off page.
    assert xml_text.count("[Contact Name 1]") >= 3


def test_missing_required_section_becomes_open_item_not_bare_heading() -> None:
    content = exemplar_content("rpa")
    del content["2.1"]  # a required prose section vanishes from the source
    xml_text = _document_xml(render.render_document("rpa", exemplar_meta("rpa"), content))
    assert "Not discussed — see Open Item #" in xml_text
    assert "Process Narrative: content was not discussed" in xml_text  # in Section 8


def test_suppressed_section_disappears_entirely() -> None:
    content = exemplar_content("rpa")
    del content["1.7"]  # empty_policy: suppress
    xml_text = _document_xml(render.render_document("rpa", exemplar_meta("rpa"), content))
    assert "1.7. Intangible Benefits" not in xml_text
    assert "Intangible Benefits: content was not discussed" not in xml_text


# ---------------------------------------------------------------------------
# Phase 1 — ingest: roster in code, stable pseudonyms, lossless chunking
# ---------------------------------------------------------------------------

_TRANSCRIPT = (
    "Meeting notes — invoice reconciliation walkthrough\n"
    "Sarah (IT): the extracts land on the shared drive at seven.\n"
    "[00:02:11] Ahmed Al-Busaidi (Finance): we pull three reports and check "
    "them line by line.\n"
    "Sarah (IT): access goes through the vault, mail me at sarah.k@corp.om.\n"
    "Note: the tolerance table lives in the rulebook.\n"
    "Speaker 3: I only join for the sign-off part.\n"
    "ahmed al-busaidi: two percent either way we accept.\n"
)


def test_roster_from_structure_zero_model() -> None:
    from sila.services.pdd import ingest

    source = ingest.normalize(_TRANSCRIPT)
    roster = ingest.build_roster(source)
    names = [s.name for s in roster]
    assert names == ["Sarah", "Ahmed Al-Busaidi", "Speaker 3"]
    by_name = {s.name: s for s in roster}
    assert by_name["Sarah"].role == "IT" and by_name["Sarah"].mentions == 2
    assert by_name["Ahmed Al-Busaidi"].role == "Finance"
    # "Note:" is prose structure, not a person.
    assert "Note" not in names


def test_pseudonyms_stable_and_applied_everywhere() -> None:
    from sila.services.pdd import ingest

    source = ingest.normalize(_TRANSCRIPT)
    pseudo = ingest.Pseudonymizer.from_roster(ingest.build_roster(source))
    assert pseudo.pseudonym_for("Sarah") == "[Contact Name 1]"
    assert pseudo.pseudonym_for("sarah") == "[Contact Name 1]"  # same person
    assert pseudo.pseudonym_for("Ahmed Al-Busaidi") == "[Contact Name 2]"
    redacted = pseudo.apply("Sarah owns this; contact sarah.k@corp.om or SARAH.")
    assert "Sarah" not in redacted and "sarah.k@corp.om" not in redacted
    assert redacted.count("[Contact Name 1]") == 2 and "[Email 1]" in redacted
    assert pseudo.entity_count == 4  # 3 names + 1 email


def test_chunker_lossless_on_40k_transcript() -> None:
    from sila.services.pdd import ingest

    lines = [f"Speaker {i % 5 + 1}: line {i} of the very long meeting.\n" for i in range(900)]
    source = ingest.normalize("".join(lines))
    assert len(source) > 40_000
    chunks = ingest.chunk_source(source)
    assert len(chunks) > 1
    covered = [False] * len(source)
    for c in chunks:
        assert c.text == source[c.start : c.end]  # spans re-base exactly
        for i in range(c.start, c.end):
            covered[i] = True
    assert all(covered)  # a 40,000-character transcript loses nothing
    # Roster still complete over the FULL text.
    assert len(ingest.build_roster(source)) == 5


# ---------------------------------------------------------------------------
# Phase 2 — extraction validator: groundedness 100%, hallucinations deleted
# ---------------------------------------------------------------------------


def _extraction_setup():
    from sila.services.pdd import extract, ingest

    source = ingest.normalize(_TRANSCRIPT)
    roster = ingest.build_roster(source)
    chunk = ingest.chunk_source(source)[0]
    result = extract.ExtractionResult()
    return extract, source, roster, chunk, result


def test_grounded_extraction_survives_and_hallucination_is_deleted() -> None:
    extract, source, roster, chunk, result = _extraction_setup()
    quote = "we pull three reports and check them line by line"
    start = source.find(quote)
    raw = {
        "2.1": {  # grounded prose — survives
            "text": "The analyst pulls three reports and checks them line by line.",
            "evidence": {
                "speaker": "Ahmed Al-Busaidi",
                "quote": quote,
                "span": [start, start + len(quote)],
            },
        },
        "2.6": [  # THE planted hallucination: a role never mentioned
            {
                "item": "The CFO approves every invoice personally",
                "evidence": {
                    "speaker": "CFO",
                    "quote": "the CFO approves everything",
                    "span": [10, 38],
                },
            }
        ],
    }
    extract.validate_chunk_output(
        raw,
        family="asis",
        chunk=chunk,
        source=source,
        roster=roster,
        doc_type="rpa",
        result=result,
    )
    assert len(result.items) == 1 and result.items[0].section_id == "2.1"
    assert len(result.dropped) == 1
    assert "not in the roster" in result.dropped[0]["reason"]
    assert result.groundedness == 0.5  # before deletion; the DOCUMENT sees 100%
    assert result.content_for_render() == {
        "2.1": "The analyst pulls three reports and checks them line by line."
    }  # the lie never reaches the renderer


def test_fabricated_quote_deleted_and_spans_derived_in_code() -> None:
    extract, source, roster, chunk, result = _extraction_setup()
    raw = {
        "2.6": [
            {  # quote does not exist anywhere in the source → deleted
                "item": "Duplicates slip through",
                "evidence": {
                    "speaker": "Sarah",
                    "quote": "duplicates slip through monthly",
                },
            },
            {  # real quote, NO span (models can't count characters — live
                # bug: demanding offsets deleted everything) → code locates it
                "item": "Extract timing",
                "evidence": {
                    "speaker": "Sarah",
                    "quote": "the extracts land on the shared drive",
                },
            },
            {  # real words, sloppy whitespace/case → normalized match keeps it
                "item": "Tolerance",
                "evidence": {
                    "speaker": "ahmed al-busaidi",
                    "quote": "Two percent  either way   we accept",
                },
            },
        ]
    }
    extract.validate_chunk_output(
        raw,
        family="asis",
        chunk=chunk,
        source=source,
        roster=roster,
        doc_type="rpa",
        result=result,
    )
    assert len(result.dropped) == 1 and "not found verbatim" in result.dropped[0]["reason"]
    assert len(result.items) == 2
    located = result.items[0].evidence.span
    assert source[located[0] : located[1]] == "the extracts land on the shared drive"


def test_required_columns_and_nfr_enum_enforced() -> None:
    extract, source, roster, chunk, result = _extraction_setup()
    quote = "the extracts land on the shared drive"
    start = source.find(quote)
    ev = {"speaker": "Sarah", "quote": quote, "span": [start, start + len(quote)]}
    raw_asis = {"2.3": [{"cells": ["1", "", "Sarah", "ERP"], "evidence": ev}]}  # nameless step
    extract.validate_chunk_output(
        raw_asis,
        family="asis",
        chunk=chunk,
        source=source,
        roster=roster,
        doc_type="rpa",
        result=result,
    )
    assert not result.items and result.dropped[0]["reason"] == "required column empty"
    raw_nfr = {"4.2": [{"cells": ["NFR-01", "Speed", "fast", "x", "Mandatory"], "evidence": ev}]}
    extract.validate_chunk_output(
        raw_nfr,
        family="requirements",
        chunk=chunk,
        source=source,
        roster=roster,
        doc_type="rpa",
        result=result,
    )
    assert any("closed enum" in d["reason"] for d in result.dropped)


def test_verb_prefix_check_per_doc_type() -> None:
    from sila.services.pdd import extract

    assert extract.check_verb_prefix("BOT should ingest the extracts", "rpa")
    assert not extract.check_verb_prefix("The bot ingests the extracts", "rpa")
    assert extract.check_verb_prefix("Macro should refresh the workbook", "macro")


def test_content_merge_dedupes_rows_across_chunks() -> None:
    extract, source, roster, chunk, result = _extraction_setup()
    quote = "two percent either way we accept"
    start = source.find(quote)
    ev = {"speaker": "ahmed al-busaidi", "quote": quote, "span": [start, start + len(quote)]}
    row = {"cells": ["1", "Match tolerance", "Ahmed", "ERP", "", "", "", "", ""], "evidence": ev}
    for _ in range(2):  # the same row arrives from two overlapping chunks
        extract.validate_chunk_output(
            {"2.3": [row]},
            family="asis",
            chunk=chunk,
            source=source,
            roster=roster,
            doc_type="rpa",
            result=result,
        )
    content = result.content_for_render()
    assert len(content["2.3"]["rows"]) == 1  # merged, not duplicated


# ---------------------------------------------------------------------------
# Phase 3 — derivation: pure code, no model
# ---------------------------------------------------------------------------

_SIX_STEP_SOURCE = (
    "Sarah (IT): the flow starts when the Purchase Order (PO) extract lands.\n"
    "Ahmed (Finance): step one, ingest the extracts from the ERP.\n"
    "Ahmed (Finance): then we match lines in SAP and log results to ServiceNow.\n"
    "Sarah (IT): the CRM feeds customer references too.\n"
)


def _derivation_setup():
    from sila.services.pdd import derive, extract, ingest
    from sila.services.pdd.fields import Evidence

    source = ingest.normalize(_SIX_STEP_SOURCE)
    roster = ingest.build_roster(source)
    result = extract.ExtractionResult()

    def _ev(quote: str) -> Evidence:
        start = source.find(quote)
        assert start >= 0
        return Evidence("Ahmed", quote, (start, start + len(quote)))

    steps = [
        ["", "Ingest extracts", "Read ERP reports", "ERP", "", "", ""],
        ["", "Validate schema", "Check columns", "ERP", "", "", ""],
        ["", "Match lines", "Apply rules", "SAP", "", "", "±2%"],
        ["", "Log results", "Write log", "ServiceNow", "", "", ""],
        ["", "Draft emails", "Compose", "Outlook", "", "", ""],
        ["", "Post file", "Write output", "SAP", "", "", ""],
    ]
    quote = "step one, ingest the extracts from the ERP"
    for row in steps:
        result.items.append(extract.ExtractedItem("3.3", row, _ev(quote)))
    # One data input naming a system nobody put in 2.4.
    result.items.append(
        extract.ExtractedItem(
            "6.1", ["", "Feed", "Customer refs", "CRM", "CSV", "id", "Internal"], _ev(quote)
        )
    )
    return derive, source, roster, result


def test_six_steps_yield_six_frs_with_verb_and_traceability() -> None:
    derive, source, roster, result = _derivation_setup()
    content, _ = derive.derive(result, source=source, roster=roster, doc_type="rpa")
    frs = content["4.1"]["rows"]
    assert len(frs) >= 6  # a six-step process cannot emit a one-row FR table
    assert all(r[2].startswith("BOT should") for r in frs)
    assert frs[0][0] == "3.3 / 1" and frs[5][1] == "FR-06"
    # Traceability: FR → speaker + real source line, in 4.3 AND Appendix B.
    assert content["4.3"]["rows"][0][0] == "FR-01" and "Ahmed" in content["4.3"]["rows"][0][1]
    assert "line 2" in content["B"]["rows"][0][2]


def test_every_named_system_lands_in_applications() -> None:
    derive, source, roster, result = _derivation_setup()
    content, _ = derive.derive(result, source=source, roster=roster, doc_type="rpa")
    apps = {r[1] for r in content["2.4"]["rows"]}
    assert {"ERP", "SAP", "ServiceNow", "Outlook", "CRM"} <= apps
    assert len(apps) == 5  # deduped


def test_raci_has_exactly_one_a_per_activity() -> None:
    import pytest as _pytest

    derive, source, roster, result = _derivation_setup()
    content, _ = derive.derive(result, source=source, roster=roster, doc_type="rpa")
    raci = content["7"]
    assert raci["columns"][0] == "Activity" and len(raci["rows"]) == 6
    for row in raci["rows"]:
        assert sum(1 for c in row[1:] if c == "A") == 1
    with _pytest.raises(ValueError, match="Accountable"):
        derive.validate_raci({"rows": [["Step", "A", "A"]]})


def test_glossary_from_source_definitions_only() -> None:
    derive, source, roster, result = _derivation_setup()
    content, undefined = derive.derive(result, source=source, roster=roster, doc_type="rpa")
    glossary = {r[0]: r[1] for r in content["6.4"]["rows"]}
    assert glossary.get("PO") == "Purchase Order"  # parenthetical in the source
    # ERP/SAP/CRM have no source definition → questions, never invented rows.
    assert {"ERP", "SAP", "CRM"} <= set(undefined)
    assert "ERP" not in glossary


def test_stakeholders_derived_from_roster_and_numbering_normalized() -> None:
    derive, source, roster, result = _derivation_setup()
    content, _ = derive.derive(result, source=source, roster=roster, doc_type="rpa")
    stakeholders = content["1.4.3"]["rows"]
    assert [r[1] for r in stakeholders] == ["Sarah", "Ahmed"]
    assert stakeholders[0][2] == "IT"  # role from the roster, not the model
    assert [r[0] for r in stakeholders] == ["1", "2"]  # '#' renumbered
    assert [r[0] for r in content["2.4"]["rows"]] == ["1", "2", "3", "4", "5"]


# ---------------------------------------------------------------------------
# Phase 4 — the gap register: export impossible around it
# ---------------------------------------------------------------------------


def test_export_blocked_until_answered_or_waived() -> None:
    import pytest as _pytest

    from sila.services.pdd import gaps

    register = gaps.build_register({}, doc_type="rpa", meta={"process_name": "Recon"})
    assert register.blockers  # 1.1/1.3/2.1/2.3/3.1/3.3 + missing department
    with _pytest.raises(ValueError, match="export blocked"):
        gaps.assert_exportable(register)
    # Answer some, waive the rest → the gate opens.
    content: dict = {}
    for gap in list(register.blockers):
        if gap.section_id == "1.1":
            gaps.apply_answer(
                register,
                content,
                gap_id=gap.id,
                answer="The process matches invoices.",
                doc_type="rpa",
            )
        else:
            gaps.waive(register, gap.id)
    gaps.assert_exportable(register)  # no raise
    assert content["1.1"] == "The process matches invoices."


def test_blocking_gaps_ranked_first_with_suggestions() -> None:
    from sila.services.pdd import gaps

    register = gaps.build_register(
        {},
        doc_type="rpa",
        undefined_acronyms=["ERP"],
        meta={"process_name": "x", "department": "y"},
    )
    assert all(g.blocking for g in register.gaps[:6])  # spine core first
    assert register.gaps[0].section_id == "1.1" and register.gaps[0].suggestion
    acronym_gap = next(g for g in register.gaps if g.section_id == "6.4")
    assert "ERP" in acronym_gap.question and not acronym_gap.blocking


def test_table_answer_parses_pipes_and_empty_answer_refused() -> None:
    import pytest as _pytest

    from sila.services.pdd import gaps

    register = gaps.build_register(
        {}, doc_type="rpa", meta={"process_name": "x", "department": "y"}
    )
    step_gap = next(g for g in register.gaps if g.section_id == "2.3")
    content: dict = {}
    gaps.apply_answer(
        register,
        content,
        gap_id=step_gap.id,
        answer="Download report | Analyst | ERP\nMatch lines | Analyst | Excel",
        doc_type="rpa",
    )
    rows = content["2.3"]["rows"]
    # '#' stays empty (auto-renumbered); user cells start at Step name.
    assert len(rows) == 2 and rows[0][0] == "" and rows[0][1] == "Download report"
    assert len(rows[0]) == 9
    with _pytest.raises(ValueError, match="empty answer"):
        gaps.apply_answer(
            register, content, gap_id=register.gaps[0].id, answer="  ", doc_type="rpa"
        )


def test_waived_gaps_render_as_open_items_not_invented_content() -> None:
    from sila.services.pdd import gaps, render
    from sila.services.pdd.exemplar import exemplar_content, exemplar_meta

    content = exemplar_content("rpa")
    del content["1.3"]  # a blocking section vanishes
    register = gaps.build_register(content, doc_type="rpa", meta=exemplar_meta("rpa"))
    gap = next(g for g in register.gaps if g.section_id == "1.3")
    gaps.waive(register, gap.id)
    gaps.assert_exportable(register)
    # The waived question rides Section 8; the body carries the honest line.
    existing = content.get("8", {}).get("rows", [])
    content["8"] = {"rows": [*existing, *gaps.open_item_rows(register, start_at=len(existing) + 1)]}
    xml_text = _document_xml(render.render_document("rpa", exemplar_meta("rpa"), content))
    assert "Waived" in xml_text
    assert "no grounded content" in xml_text  # the question, in Section 8
    assert "Not discussed — see Open Item #" in xml_text  # the body line


def test_coverage_metric_moves_with_answers() -> None:
    from sila.services.pdd import gaps
    from sila.services.pdd.exemplar import exemplar_content

    full = gaps.coverage(exemplar_content("rpa"), "rpa")
    assert full == 1.0
    partial = dict(exemplar_content("rpa"))
    del partial["1.1"], partial["2.3"]
    assert gaps.coverage(partial, "rpa") < full


# ---------------------------------------------------------------------------
# Phase 5 — figures from JSON, redaction at the export boundary
# ---------------------------------------------------------------------------


def test_flowcharts_generated_from_structured_steps() -> None:
    from sila.services.pdd import render
    from sila.services.pdd.exemplar import exemplar_content, exemplar_meta

    content = exemplar_content("rpa")
    del content["2.2"], content["3.2"]  # no hand-written figure payloads
    xml_text = _document_xml(render.render_document("rpa", exemplar_meta("rpa"), content))
    # The figures materialize FROM 2.3/3.3 rows — never a placeholder box.
    assert "AS-IS process flow — generated from §2.3" in xml_text
    assert "TO-BE process flow — generated from §3.3" in xml_text
    assert "1. Download open invoices" in xml_text  # a real step box
    assert xml_text.count("▼") >= 8  # connectors between boxes


def test_raci_renders_as_color_coded_grid() -> None:
    from sila.services.pdd import render
    from sila.services.pdd.exemplar import exemplar_content, exemplar_meta

    xml_text = _document_xml(
        render.render_document("rpa", exemplar_meta("rpa"), exemplar_content("rpa"))
    )
    assert '<w:shd w:val="clear" w:fill="1F2A44"/>' in xml_text  # A cells
    assert '<w:shd w:val="clear" w:fill="3E6DB5"/>' in xml_text  # R cells


def test_export_pipeline_gates_then_redacts() -> None:
    import pytest as _pytest

    from sila.services.pdd import gaps, ingest, pipeline
    from sila.services.pdd.exemplar import exemplar_content, exemplar_meta

    source = ingest.normalize(_TRANSCRIPT)
    roster = ingest.build_roster(source)
    pseudo = ingest.Pseudonymizer.from_roster(roster)
    content = exemplar_content("rpa")
    content["2.1"] = "Sarah downloads the report and mails ahmed al-busaidi at sarah.k@corp.om."
    register = gaps.build_register(content, doc_type="rpa", meta=exemplar_meta("rpa"))

    # Plant a blocking gap → export must be impossible.
    del content["3.1"]
    blocked_register = gaps.build_register(content, doc_type="rpa", meta=exemplar_meta("rpa"))
    with _pytest.raises(ValueError, match="export blocked"):
        pipeline.export_document(
            doc_type="rpa",
            meta=exemplar_meta("rpa"),
            content=content,
            register=blocked_register,
            pseudonymizer=pseudo,
        )

    # Waive it → the export renders, fully redacted.
    for gap in list(blocked_register.blockers):
        gaps.waive(blocked_register, gap.id)
    doc = pipeline.export_document(
        doc_type="rpa",
        meta=exemplar_meta("rpa"),
        content=content,
        register=blocked_register,
        pseudonymizer=pseudo,
    )
    xml_text = _document_xml(doc)
    assert "Sarah" not in xml_text and "sarah.k@corp.om" not in xml_text
    assert "[Contact Name 1]" in xml_text and "[Email 1]" in xml_text
    assert "entities pseudonymized" in xml_text  # Appendix C, derived count
    assert "Waived" in xml_text  # the waived gap is an open item, not invented
    del register  # unused clean reference


# ---------------------------------------------------------------------------
# Phase 6 — refine loop: typed patches, automatic consistency
# ---------------------------------------------------------------------------


def test_owner_change_alters_one_field_and_raci_updates() -> None:
    from sila.services.pdd import refine
    from sila.services.pdd.exemplar import exemplar_content, exemplar_meta

    content = exemplar_content("rpa")
    meta = exemplar_meta("rpa")
    before = {k: str(v) for k, v in content.items() if k != "7"}
    # "Change the owner of the posting-confirmation step to [Contact Name 3]."
    changes = refine.apply_patches(
        content,
        meta,
        [
            {
                "op": "set_raci",
                "activity": "Produce posting file",
                "person": "[Contact Name 3]",
                "letter": "A",
            }
        ],
        doc_type="rpa",
    )
    row = next(r for r in content["7"]["rows"] if r[0] == "Produce posting file")
    assert row[3] == "A" and row[1] == "C"  # promoted; previous A demoted
    from sila.services.pdd.derive import validate_raci

    validate_raci(content["7"])  # still exactly one A
    # …and NOTHING else moved.
    assert {k: str(v) for k, v in content.items() if k != "7"} == before
    assert any("demoted" in c for c in changes)


def test_step_rename_syncs_raci_and_regenerates_figure() -> None:
    from sila.services.pdd import refine
    from sila.services.pdd.exemplar import exemplar_content, exemplar_meta

    content = exemplar_content("rpa")
    changes = refine.apply_patches(
        content,
        exemplar_meta("rpa"),
        [
            {
                "op": "set_cell",
                "section": "3.3",
                "row": 2,
                "column": "Step",
                "value": "Three-way match with tolerances",
            }
        ],
        doc_type="rpa",
    )
    assert content["7"]["rows"][1][0] == "Three-way match with tolerances"
    assert "3.2" not in content  # figure dropped → regenerates from rows
    assert any("regenerate" in c for c in changes)


def test_batch_is_atomic_and_bad_patches_are_loud() -> None:
    import pytest as _pytest

    from sila.services.pdd import refine
    from sila.services.pdd.exemplar import exemplar_content, exemplar_meta

    content = exemplar_content("rpa")
    meta = exemplar_meta("rpa")
    snapshot = str(content)
    with _pytest.raises(refine.PatchError, match="no column"):
        refine.apply_patches(
            content,
            meta,
            [
                {"op": "set_prose", "section": "3.1", "text": "New overview."},
                {"op": "set_cell", "section": "2.3", "row": 1, "column": "Ghost", "value": "x"},
            ],
            doc_type="rpa",
        )
    assert str(content) == snapshot  # the GOOD patch didn't half-apply either
    with _pytest.raises(refine.PatchError, match="required"):
        refine.apply_patch(
            content,
            meta,
            {"op": "add_row", "section": "2.3", "cells": ["9", "", "Nobody"]},
            doc_type="rpa",
        )  # a nameless step still fails validation at the patch layer


# ---------------------------------------------------------------------------
# Phases 7+8 — SILA wiring end-to-end (fake LLM) + the quality gate
# ---------------------------------------------------------------------------


def _pdd_ctx() -> object:
    import uuid as _uuid
    from unittest.mock import AsyncMock

    from sila.adapters.auth.base import UserResult
    from sila.core.constants import Role
    from sila.services.pdd import ingest
    from sila.services.sila.tool_registry import ToolContext

    ctx = ToolContext(
        db=AsyncMock(),
        user=UserResult(id=_uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        workspace_id=_uuid.uuid4(),
        role=Role.VIEWER,
    )
    padding = "Sarah (IT): " + "the rulebook covers the details here. " * 8 + "\n"
    ctx.build_state["uploaded_files"] = [
        {"name": "meeting.txt", "content": ingest.normalize(_TRANSCRIPT + padding)}
    ]
    return ctx


class _FakePddLLM:
    def __init__(self, *a: object, **k: object) -> None: ...

    async def complete_json(self, *, system_prompt: str, user_prompt: str, timeout_s: float = 0):
        text = user_prompt.split("\n", 1)[1]

        def ev(quote: str) -> dict:
            start = text.find(quote)
            return {
                "speaker": "Ahmed Al-Busaidi",
                "quote": quote,
                "span": [start, start + len(quote)],
            }

        q = "we pull three reports and check them line by line"
        if "CURRENT manual process" in system_prompt:
            return {
                "2.1": {"text": "The analyst matches three reports by hand.", "evidence": ev(q)},
                "2.3": [
                    {
                        "cells": ["1", "Pull reports", "Ahmed", "ERP", "", "", "", "", "Manual"],
                        "evidence": ev(q),
                    }
                ],
            }
        if "PROPOSED automation" in system_prompt:
            return {
                "3.1": {"text": "A bot ingests and matches the reports.", "evidence": ev(q)},
                "3.3": [
                    {
                        "cells": ["1", "Ingest reports", "Read extracts", "ERP", "", "", ""],
                        "evidence": ev(q),
                    },
                    {
                        "cells": ["2", "Match lines", "Apply rules", "ERP", "", "", "±2%"],
                        "evidence": ev(q),
                    },
                ],
            }
        return {}


async def test_pdd_tools_end_to_end_with_gate_and_redaction(monkeypatch) -> None:
    from sila.services.sila.tool_registry import execute_tool

    monkeypatch.setattr("sila.adapters.llm.ollama.OllamaLLMAdapter", _FakePddLLM)
    ctx = _pdd_ctx()

    started = await execute_tool(
        "pdd_start", ctx, {"doc_type": "rpa", "process_name": "Invoice Recon"}
    )
    assert "ROSTER" in started.content and "Sarah" in started.content

    out = await execute_tool("pdd_extract", ctx, {})
    assert "QUALITY REPORT" in out.content and "Groundedness 100%" in out.content
    assert "BLOCKING" in out.content  # 1.1/1.3 have no grounded content

    # THE gate: export refuses while blocking questions are open.
    blocked = await execute_tool("pdd_export", ctx, {})
    assert "EXPORT BLOCKED" in blocked.content

    # Answer one, waive the rest.
    state = ctx.build_state["pdd"]
    blockers = [g for g in state["gaps"] if g["blocking"] and g["status"] == "open"]
    first = await execute_tool(
        "pdd_answer_gap",
        ctx,
        {"gap_id": blockers[0]["id"], "answer": "It protects us from double payment."},
    )
    assert "open item(s) left" in first.summary
    for gap in blockers[1:]:
        await execute_tool("pdd_answer_gap", ctx, {"gap_id": gap["id"], "waive": True})

    captured: dict = {}

    class _FakeFiles:
        def __init__(self, _db: object) -> None: ...

        async def create(self, **kw: object) -> object:
            import uuid as _uuid

            captured.update(kw)
            return type("Row", (), {"id": _uuid.uuid4()})()

    monkeypatch.setattr("sila.db.repositories.generated_files.GeneratedFileRepository", _FakeFiles)
    done = await execute_tool("pdd_export", ctx, {})
    # The file rides the RESULT as an attachment now, not a URL pasted into the
    # prose for the model to relay — so assert the artifact, not a "DOWNLOAD" line.
    assert "QUALITY REPORT — Coverage" in done.content
    exported = (done.meta or {}).get("artifacts") or []
    assert exported and exported[0]["kind"] == "doc" and exported[0]["url"]
    xml_text = _document_xml(captured["file_bytes"])
    assert "Ahmed Al-Busaidi" not in xml_text  # redacted at the boundary
    assert "[Contact Name 2]" in xml_text
    assert "FR-01" in xml_text and "BOT should" in xml_text  # derived FRs shipped
    assert "Waived" in xml_text  # the waived gaps are open items


async def test_pdd_edit_tool_uses_typed_patches(monkeypatch) -> None:
    from sila.services.sila.tool_registry import execute_tool

    monkeypatch.setattr("sila.adapters.llm.ollama.OllamaLLMAdapter", _FakePddLLM)
    ctx = _pdd_ctx()
    await execute_tool("pdd_start", ctx, {"doc_type": "rpa", "process_name": "X"})
    await execute_tool("pdd_extract", ctx, {})
    res = await execute_tool(
        "pdd_edit",
        ctx,
        {
            "patches": [
                {"op": "set_raci", "activity": "Match lines", "person": "Sarah", "letter": "A"}
            ]
        },
    )
    assert "→ A on 'Match lines'" in res.content
    bad = await execute_tool(
        "pdd_edit",
        ctx,
        {"patches": [{"op": "set_cell", "section": "9.9", "row": 1, "column": "x", "value": "y"}]},
    )
    assert "Nothing was changed" in bad.content


def test_pdd_template_registered_with_persona_whitelist() -> None:
    from sila.services.bazaar_templates import TEMPLATES, list_template_payloads, persona_for

    assert "tpl-pdd" in TEMPLATES
    assert persona_for("tpl-pdd").startswith("ACTIVE TEMPLATE — PROCESS DESIGN")
    payload = next(p for p in list_template_payloads() if p["id"] == "tpl-pdd")
    assert "persona" not in payload and payload["kind"] == "chat"


def test_gap_resolves_by_section_and_waive_all() -> None:
    from sila.services.pdd import gaps, session

    state = {
        "doc_type": "rpa",
        "meta": {"process_name": "X", "department": "Y"},
        "content": {},
        "gaps": [],
        "roster": [],
        "source": "x",
    }
    register = gaps.build_register({}, doc_type="rpa", meta=state["meta"])
    state["gaps"] = (
        [gaps.asdict(g) for g in register.gaps]
        if hasattr(gaps, "asdict")
        else [g.__dict__ for g in register.gaps]
    )
    # Answer by SECTION NUMBER (the model dropped the gap ids — live bug).
    q = session.answer_gap(state, gap_id="1.1", answer="It reconciles vendor invoices.")
    assert state["content"]["1.1"] == "It reconciles vendor invoices."
    assert q["coverage_pct"] > 0
    # A bad reference lists the open gaps instead of a bare KeyError.
    import pytest as _pytest

    with _pytest.raises(KeyError, match="open gaps"):
        session.answer_gap(state, gap_id="9.9", answer="x")
    # "skip the rest" = ONE call.
    q2 = session.answer_gap(state, gap_id="all")
    assert q2["open_items"] == 0 and not q2["blocking_questions"]


# ---------------------------------------------------------------------------
# Live-test round 2 — the fixes that made extraction survive
# ---------------------------------------------------------------------------


def test_speaker_with_role_suffix_and_punctuation_tolerant_quotes() -> None:
    """Live bug: 45 extracted, 45 deleted — qwen wrote 'Layla (Process Owner)'
    as the speaker and normalized punctuation inside quotes."""
    extract, source, roster, chunk, result = _extraction_setup()
    raw = {
        "2.6": [
            {
                "item": "Vault access",
                "evidence": {
                    "speaker": "Sarah (IT)",  # role suffix — must match Sarah
                    "quote": "access goes through the vault",
                },
            },
            {
                "item": "Line-by-line checking",
                "evidence": {
                    "speaker": "Ahmed Al-Busaidi (Finance)",
                    # dash instead of the source's spaces — word sequence intact
                    "quote": "check them line-by-line",
                },
            },
            {
                "item": "Still a lie",
                "evidence": {"speaker": "The CFO", "quote": "access goes through the vault"},
            },
        ]
    }
    extract.validate_chunk_output(
        raw,
        family="asis",
        chunk=chunk,
        source=source,
        roster=roster,
        doc_type="rpa",
        result=result,
    )
    assert len(result.items) == 2  # both real speakers survive
    assert len(result.dropped) == 1 and "CFO" in result.dropped[0]["reason"]


def test_pipe_answer_skips_the_hash_column() -> None:
    from sila.services.pdd import gaps

    register = gaps.build_register(
        {}, doc_type="rpa", meta={"process_name": "x", "department": "y"}
    )
    objectives = next(g for g in register.gaps if g.section_id == "1.3")
    content: dict = {}
    gaps.apply_answer(
        register,
        content,
        gap_id=objectives.id,
        answer="cut cycle to 2h | cycle time in run log",
        doc_type="rpa",
    )
    row = content["1.3"]["rows"][0]
    assert row[0] == ""  # '#' left for renumbering
    assert row[1] == "cut cycle to 2h" and row[2] == "cycle time in run log"


def test_glossary_definition_is_title_cased_and_acronyms_capped() -> None:
    from sila.services.pdd import derive, extract, ingest

    source = ingest.normalize(
        "Layla (Owner): then I export the Purchase Order (PO) data from SAP.\n"
        "Layla (Owner): the IT team pays in OMR via ERP, CRM, WMS, TMS, LMS, BPM systems.\n"
    )
    content, undefined = derive.derive(
        extract.ExtractionResult(),
        source=source,
        roster=ingest.build_roster(source),
        doc_type="rpa",
    )
    glossary = {r[0]: r[1] for r in content["6.4"]["rows"]}
    assert glossary["PO"] == "Purchase Order"  # not "I export the Purchase Order"
    assert "IT" not in undefined and "OMR" not in undefined  # stoplist
    assert len(undefined) <= 5  # capped


def test_waived_gap_renders_once_not_twice() -> None:
    """Live bug: the export carried a Waived register row AND a twin auto
    'content was not discussed' row for the same section."""
    from sila.services.pdd import gaps, render
    from sila.services.pdd.exemplar import exemplar_content, exemplar_meta

    content = exemplar_content("rpa")
    del content["2.1"]
    register = gaps.build_register(content, doc_type="rpa", meta=exemplar_meta("rpa"))
    gap = next(g for g in register.gaps if g.section_id == "2.1")
    gaps.waive(register, gap.id)
    existing = content.get("8", {}).get("rows", [])
    content["8"] = {"rows": [*existing, *gaps.open_item_rows(register, start_at=len(existing) + 1)]}
    waived_number = next(r[0] for r in content["8"]["rows"] if r[1] == "2.1" and r[3] == "Waived")
    xml_text = _document_xml(render.render_document("rpa", exemplar_meta("rpa"), content))
    assert xml_text.count("Process Narrative: content was not discussed") == 0  # no twin
    assert f"Not discussed — see Open Item #{waived_number}." in xml_text  # right ref


# ---------------------------------------------------------------------------
# Live-test round 3 — the router hijack and the silent reset
# ---------------------------------------------------------------------------


async def test_propose_plan_refuses_inside_a_template_session() -> None:
    """Live bug: a PDD gap answer got routed into the generic agent planner
    ('Building the agent'). The plan card is a code-level no-go in ANY
    pinned-template session."""
    from sila.services.sila.tool_registry import execute_tool

    ctx = _pdd_ctx()
    ctx.build_state["active_template"] = "tpl-pdd"
    res = await execute_tool("propose_plan", ctx, {"goal": "automate reconciliation"})
    assert "Template session" in (res.summary or "")
    assert "staged flow" in res.content


def test_pdd_template_has_a_tool_whitelist() -> None:
    from sila.services.bazaar_templates import tools_for

    tools = tools_for("tpl-pdd")
    assert "pdd_start" in tools and "pdd_export" in tools
    assert "propose_plan" not in tools and "create_data_dashboard" not in tools
    assert tools_for("tpl-data-analyst") == ()  # analyst keeps the full set
    assert tools_for("unknown") == ()


async def test_pdd_start_refuses_silent_reset(monkeypatch) -> None:
    """Live bug: a repeated 'yes, go ahead' re-ingested the transcript and
    the extraction changed under the user's feet (15 kept → 13)."""
    from sila.services.sila.tool_registry import execute_tool

    monkeypatch.setattr("sila.adapters.llm.ollama.OllamaLLMAdapter", _FakePddLLM)
    ctx = _pdd_ctx()
    await execute_tool("pdd_start", ctx, {"doc_type": "rpa", "process_name": "X"})
    await execute_tool("pdd_extract", ctx, {})
    kept_before = ctx.build_state["pdd"]["extracted_count"]
    again = await execute_tool("pdd_start", ctx, {"doc_type": "rpa", "process_name": "X"})
    assert "already" in again.content.lower()
    assert ctx.build_state["pdd"]["extracted_count"] == kept_before  # untouched
    reset = await execute_tool(
        "pdd_start", ctx, {"doc_type": "rpa", "process_name": "X", "reset": True}
    )
    assert "ROSTER" in reset.content  # explicit reset still works


def test_model_sees_transcript_preview_but_stash_keeps_all() -> None:
    """Live bug chain: the composer's 6k cap cut a 17k transcript (roster lost
    speakers); lifting it must NOT dump 17k chars into the model prompt."""
    from sila.services.pdd import ingest
    from sila.services.sila.orchestrator.runner import (
        _shrink_tabular_attachments,
        stash_attachments,
    )

    body = "".join(f"Speaker {i % 5 + 1}: line {i} of the meeting.\n" for i in range(700))
    message = f"document this\n[Attached file: meeting.txt]\n{body}\n[End of meeting.txt]"
    state: dict = {}
    stash_attachments(message, state)
    stashed = state["uploaded_files"][0]["content"]
    assert len(ingest.build_roster(ingest.normalize(stashed))) == 5  # nothing lost
    shrunk = _shrink_tabular_attachments(message)
    assert len(shrunk) < 3_000  # the MODEL gets a preview, not the transcript
    assert "FULL text is available to the document tools" in shrunk


async def test_skip_and_export_is_one_call(monkeypatch) -> None:
    """Live bug: 'skip them and export' looped on a blocked export — the model
    never waived. waive_open=true makes it a single deterministic call."""
    from sila.services.sila.tool_registry import execute_tool

    monkeypatch.setattr("sila.adapters.llm.ollama.OllamaLLMAdapter", _FakePddLLM)
    ctx = _pdd_ctx()
    await execute_tool("pdd_start", ctx, {"doc_type": "rpa", "process_name": "X"})
    await execute_tool("pdd_extract", ctx, {})
    blocked = await execute_tool("pdd_export", ctx, {})
    assert "EXPORT BLOCKED" in blocked.content and "waive_open=true" in blocked.content

    captured: dict = {}

    class _FakeFiles:
        def __init__(self, _db: object) -> None: ...

        async def create(self, **kw: object) -> object:
            import uuid as _uuid

            captured.update(kw)
            return type("Row", (), {"id": _uuid.uuid4()})()

    monkeypatch.setattr("sila.db.repositories.generated_files.GeneratedFileRepository", _FakeFiles)
    done = await execute_tool("pdd_export", ctx, {"waive_open": True})
    exported = (done.meta or {}).get("artifacts") or []
    assert exported and exported[0]["url"], "the export must attach the document"
    assert captured.get("file_bytes")
    assert all(g["status"] != "open" for g in ctx.build_state["pdd"]["gaps"])


def test_requirements_prompt_names_the_closed_nfr_enum() -> None:
    from sila.services.pdd import extract

    rpa = extract.family_prompt("requirements", "rpa")
    macro = extract.family_prompt("requirements", "macro")
    assert "MUST be one of exactly: Performance, Security, Availability" in rpa
    assert "Compatibility" in macro and "Availability" not in macro
