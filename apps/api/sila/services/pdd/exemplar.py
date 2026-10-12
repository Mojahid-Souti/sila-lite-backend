"""PDD Template — the styled empty exemplar (Phase 0 done-when).

Placeholder-but-meaningful content for EVERY section of both document types,
so the rendered exemplar proves the base style end-to-end: no bare headings,
no all-dash tables, pseudonyms consistent across stakeholders → RACI →
sign-off (the spec's rule 5). Real pipelines replace this content; the
renderer and registry stay identical.
"""

from __future__ import annotations

from typing import Any

_P1, _P2, _P3 = "[Contact Name 1]", "[Contact Name 2]", "[Contact Name 3]"


def exemplar_meta(doc_type: str) -> dict[str, Any]:
    return {
        "process_name": "Monthly Vendor Invoice Reconciliation",
        "doc_type": doc_type,
        "version": "0.1",
        "author": _P1,
        "department": "Finance Operations",
        "date": "2026-07-12",
        "classification": "CONFIDENTIAL",
        "abstract": (
            "This document specifies the automation of the monthly vendor invoice "
            "reconciliation process: matching supplier invoices against purchase "
            "orders and goods receipts, flagging mismatches for review, and posting "
            "matched invoices for payment — reducing a two-day manual effort to an "
            "attended run measured in minutes."
        ),
        "tags": ["finance", "reconciliation", "exemplar"],
        "status": "Draft",
    }


def exemplar_content(doc_type: str) -> dict[str, Any]:
    """Every enabled section populated (macro sections included when asked)."""
    content: dict[str, Any] = {
        "front.contributors": {
            "rows": [
                [_P1, "Process Owner — Finance Operations", "Process walkthrough, KPIs"],
                [_P2, "Finance Analyst", "AS-IS steps, exception cases"],
                [_P3, "IT Administrator", "Systems, access, environments"],
            ]
        },
        "front.signoff": {
            "rows": [
                [_P1, "Process Owner", "S", "", ""],
                [_P2, "Finance Analyst", "R", "", ""],
                [_P3, "IT Administrator", "I", "", ""],
            ]
        },
        "1.1": (
            "Vendor invoice reconciliation matches incoming supplier invoices against "
            "purchase orders and goods receipts before payment. It protects the "
            "company from over-payment and duplicate payment, and is currently a "
            "fully manual, month-end activity in Finance Operations."
        ),
        "1.2": [
            "Two full working days of manual matching every month-end",
            "Copy-paste between the ERP and spreadsheets introduces transcription errors",
            "Duplicate invoices are caught late, after payment in some cases",
        ],
        "1.3": {
            "rows": [
                ["1", "Reduce reconciliation cycle time", "Cycle time ≤ 2 hours per run"],
                ["2", "Eliminate duplicate payments", "Zero duplicates post-automation"],
                ["3", "Free analyst capacity", "≥ 12 analyst-days returned per year"],
            ]
        },
        "1.4": (
            "The automation covers the matching and flagging of vendor invoices for "
            "one legal entity, from invoice receipt to posting recommendation. "
            "Payment execution remains a human decision."
        ),
        "1.4.1": [
            "Three-way match: invoice ↔ purchase order ↔ goods receipt",
            "Mismatch flagging with reason codes",
            "Posting recommendation file for approved matches",
        ],
        "1.4.2": [
            "Payment execution and bank interfaces",
            "Non-PO (direct expense) invoices",
            "Vendor master data maintenance",
        ],
        "1.4.3": {
            "rows": [
                ["1", _P1, "Process Owner", "Finance Operations", "A", "[email 1]"],
                ["2", _P2, "Finance Analyst", "Finance Operations", "R", "[email 2]"],
                ["3", _P3, "IT Administrator", "IT Services", "C", "[email 3]"],
            ]
        },
        "1.5": {
            "rows": [
                [
                    "Cost per reconciliation",
                    "Fully-loaded cost of one month-end run",
                    "Analyst hours × hourly rate",
                    "OMR 480 / month",
                    "OMR 60 / month",
                    "Finance controlling",
                ]
            ]
        },
        "1.6": {
            "rows": [
                [
                    "Cycle time",
                    "Elapsed time per month-end run",
                    "End timestamp − start timestamp",
                    "16 hours",
                    "2 hours",
                    "Run log",
                ],
                [
                    "First-pass match rate",
                    "Invoices matched without human touch",
                    "Auto-matched ÷ total invoices",
                    "— (not measured)",
                    "≥ 85%",
                    "Run log",
                ],
            ]
        },
        "1.7": {
            "rows": [
                ["1", "Analyst morale", "Removes the most repetitive month-end task"],
                ["2", "Audit readiness", "Every match decision is logged and traceable"],
            ]
        },
        "2.1": (
            "Today the analyst downloads the open-invoice report from the ERP, "
            "exports purchase orders and goods receipts to a spreadsheet, and matches "
            "line items by hand. Mismatches are emailed to buyers; matched invoices "
            "are posted one by one."
        ),
        "2.2": {"note": "Figure: AS-IS process map — generated from the steps in §2.3."},
        "2.3": {
            "rows": [
                [
                    "1",
                    "Download open invoices",
                    _P2,
                    "ERP",
                    "Report filters",
                    "Invoice list",
                    "20 min",
                    "Monthly",
                    "Manual",
                ],
                [
                    "2",
                    "Export PO and GR data",
                    _P2,
                    "ERP",
                    "Invoice list",
                    "PO/GR extract",
                    "30 min",
                    "Monthly",
                    "Manual",
                ],
                [
                    "3",
                    "Match line items",
                    _P2,
                    "Excel",
                    "Both extracts",
                    "Match workbook",
                    "10 h",
                    "Monthly",
                    "Manual",
                ],
                [
                    "4",
                    "Email mismatches to buyers",
                    _P2,
                    "Outlook",
                    "Match workbook",
                    "Clarification emails",
                    "2 h",
                    "Monthly",
                    "Manual",
                ],
                [
                    "5",
                    "Post matched invoices",
                    _P2,
                    "ERP",
                    "Match workbook",
                    "Posted invoices",
                    "3 h",
                    "Monthly",
                    "Manual",
                ],
            ]
        },
        "2.4": {
            "rows": [
                ["1", "ERP (financials)", "Production", "Named user", _P3],
                ["2", "Microsoft Excel", "M365", "Desktop", _P3],
                ["3", "Microsoft Outlook", "M365", "Desktop", _P3],
            ]
        },
        "2.5": {
            "rows": [
                ["1", "Invoices reconciled", "≈ 1,150", "invoices", "Monthly", "Month-end +3 days"],
            ]
        },
        "2.6": [
            "Match rules live in the analyst's head — no documented rulebook",
            "No audit trail of why an invoice was cleared",
        ],
        "2.7": {
            "rows": [
                [
                    "1",
                    "Partial delivery",
                    "GR quantity < PO quantity",
                    "Manual judgement call",
                    "≈ 40 / month",
                ],
                [
                    "2",
                    "Price variance",
                    "Invoice price ≠ PO price",
                    "Email to buyer",
                    "≈ 25 / month",
                ],
            ]
        },
        "3.1": (
            "The automation ingests the three source extracts, applies the documented "
            "match rules, flags exceptions with reason codes, and produces a posting "
            "recommendation. A human approves exceptions; everything else flows "
            "straight through."
        ),
        "3.2": {"note": "Figure: TO-BE process map — generated from the steps in §3.3."},
        "3.3": {
            "rows": [
                [
                    "1",
                    "Ingest source extracts",
                    "Read ERP reports",
                    "ERP",
                    "Report files",
                    "Normalized tables",
                    "Schema check",
                ],
                [
                    "2",
                    "Three-way match",
                    "Apply match rules",
                    "—",
                    "Normalized tables",
                    "Match result",
                    "Tolerance ±2%",
                ],
                [
                    "3",
                    "Flag exceptions",
                    "Assign reason codes",
                    "—",
                    "Match result",
                    "Exception queue",
                    "Rulebook v1",
                ],
                [
                    "4",
                    "Draft buyer emails",
                    "Compose from template",
                    "Outlook",
                    "Exception queue",
                    "Draft emails",
                    "One per buyer",
                ],
                [
                    "5",
                    "Produce posting file",
                    "Write recommendation",
                    "ERP",
                    "Match result",
                    "Posting file",
                    "Matched only",
                ],
            ]
        },
        "3.4": {
            "rows": [
                ["1", "Exception approval", _P2, "Judgement on variances", "Same business day"],
                ["2", "Posting confirmation", _P1, "Financial accountability", "Next business day"],
            ]
        },
        "3.5": {
            "rows": [
                [
                    "1",
                    "Source extract missing",
                    "File absent at cut-off",
                    "Retry ×3, then halt",
                    _P3,
                    "1 hour",
                ],
                [
                    "2",
                    "Unknown vendor",
                    "Vendor not in master data",
                    "Route to exception queue",
                    _P2,
                    "Same day",
                ],
            ]
        },
        "3.6": (
            "Runs on the second working day after month-end close, 07:00 local time, "
            "finishing before the 10:00 payment-run cut-off. No runs on public holidays; "
            "the schedule follows the finance close calendar."
        ),
        "3.7": [
            "Rules-based: yes — documented three-way match with tolerances",
            "Structured input: yes — three tabular ERP extracts",
            "System stability: high — ERP report layer is versioned",
            "Volume: ≈ 1,150 invoices monthly, peak at month-end +3 days",
            "Complexity rating: Medium",
        ],
        "4.1": {
            "rows": [
                [
                    "3.3 / 1",
                    "FR-01",
                    "BOT should ingest the three source extracts and validate their schemas",
                    "Halt on schema drift",
                    "High",
                    "Mandatory",
                ],
                [
                    "3.3 / 2",
                    "FR-02",
                    "BOT should apply the three-way match rules with a ±2% price tolerance",
                    "Rulebook v1",
                    "High",
                    "Mandatory",
                ],
                [
                    "3.3 / 3",
                    "FR-03",
                    "BOT should assign a reason code to every exception",
                    "Closed reason-code list",
                    "High",
                    "Mandatory",
                ],
                [
                    "3.3 / 4",
                    "FR-04",
                    "BOT should draft buyer clarification emails from the approved template",
                    "One email per buyer per run",
                    "Medium",
                    "Mandatory",
                ],
                [
                    "3.3 / 5",
                    "FR-05",
                    "BOT should produce the posting recommendation file for matched invoices",
                    "Matched invoices only",
                    "High",
                    "Mandatory",
                ],
            ]
        },
        "4.2": {
            "rows": [
                [
                    "NFR-01",
                    "Performance",
                    "A full run completes within 2 hours",
                    "Run log duration ≤ 2 h",
                    "Mandatory",
                ],
                [
                    "NFR-02",
                    "Security",
                    "Credentials come from the enterprise vault",
                    "No plaintext credentials",
                    "Mandatory",
                ],
            ]
        },
        "4.3": {
            "rows": [
                ["FR-01", f"{_P2} — transcript line 41", _P1],
                ["FR-02", f"{_P2} — transcript line 58", _P1],
            ]
        },
        "5.1": {
            "rows": [
                ["1", "ERP report layouts remain stable", "Match rules mis-read the extracts"],
                ["2", "Buyers answer clarification emails within 2 days", "Exception queue ages"],
            ]
        },
        "5.2": {
            "rows": [
                [
                    "1",
                    "ERP report layer changes without notice",
                    "Medium",
                    "High",
                    "Schema check halts the run",
                    _P3,
                ],
                [
                    "2",
                    "Tolerance masks a real pricing error",
                    "Low",
                    "Medium",
                    "Monthly sample audit",
                    _P1,
                ],
            ]
        },
        "5.3": {
            "rows": [
                ["1", "Documented match rulebook v1", _P1, "Before build start"],
            ]
        },
        "5.4": (
            "The process touches supplier names, bank references, and invoice amounts "
            "— Internal classification. No personal data of natural persons beyond "
            "business contact details. Extracts are retained 90 days, then purged. "
            "PDPL: business-contact processing under legitimate interest; no "
            "cross-border transfer."
        ),
        "6.1": {
            "rows": [
                ["1", "Report", "Open invoice report", "ERP", "XLSX", "Invoice number", "Internal"],
                ["2", "Report", "PO / GR extract", "ERP", "XLSX", "PO number", "Internal"],
            ]
        },
        "6.2": {
            "rows": [
                [
                    "1",
                    "Posting recommendation",
                    "ERP inbox folder",
                    "CSV",
                    "Monthly",
                    "Finance Operations",
                ],
                ["2", "Exception report", "Shared drive", "XLSX", "Monthly", _P2],
            ]
        },
        "6.3": (
            "Working files live in the automation's controlled folder, retained 90 "
            "days for audit, then deleted. No data leaves the corporate network."
        ),
        "6.4": {
            "rows": [
                ["PO", "Purchase Order"],
                ["GR", "Goods Receipt"],
                ["Three-way match", "Invoice ↔ PO ↔ GR comparison before payment"],
            ]
        },
        "6.5": ["Finance close calendar 2026", "Procurement policy v3"],
        "7": {
            "note": "Figure: RACI matrix — generated from §3.3 activities × §1.4.3 roles.",
            "columns": ["Activity", _P1, _P2, _P3],
            "rows": [
                ["Ingest source extracts", "I", "R", "A"],
                ["Three-way match", "A", "R", "I"],
                ["Flag exceptions", "A", "R", "I"],
                ["Draft buyer emails", "I", "A", "I"],
                ["Produce posting file", "A", "R", "C"],
            ],
        },
        "8": {
            "rows": [
                ["1", "1.5", "Confirm the baseline cost figure with controlling", "Open", _P1],
            ]
        },
        "A": (
            "Subject: Vendor invoice reconciliation — automation design for your review\n\n"
            "Dear colleague, attached is the design document for the reconciliation "
            "automation. Sections 3 and 4 describe what changes for your team; "
            "please review your rows in the sign-off table."
        ),
        "B": {
            "rows": [
                [
                    "FR-01",
                    _P2,
                    "line 41: “we pull three reports and check them line by line”",
                ],
                ["FR-02", _P2, "line 58: “two percent either way we accept”"],
            ]
        },
        "C": (
            "3 entities pseudonymized. The mapping between pseudonyms and real "
            "identities is held by the document owner and is not part of this file."
        ),
        "D": {"rows": [["0.1", "Initial exemplar", _P1, "2026-07-12"]]},
    }
    if doc_type == "macro":
        content["3.8"] = [
            "Language: VBA (Office 64-bit)",
            "Host application: Microsoft Excel, Microsoft 365 (semi-annual channel)",
            "File format: .xlsm (macro-enabled workbook)",
            "Workbook structure: Input | staging for the three extracts; "
            "Rules | tolerance table; Output | posting file",
            "References: Microsoft Scripting Runtime; no third-party libraries",
            "Trigger method: ribbon button on the Output sheet (attended)",
            "Performance: arrays over cell-by-cell writes; screen updating off during runs",
        ]
        content["4.4"] = [
            "Macro security: signed macros only; Trust Center blocks unsigned code",
            "Digital signature: departmental code-signing certificate",
            "Distribution: read-only master on the finance shared drive",
            "Naming & version: Reconciliation_vMAJOR.MINOR.xlsm; version sheet in workbook",
            "Protected View: master location added to Trusted Locations by IT",
        ]
    return content
