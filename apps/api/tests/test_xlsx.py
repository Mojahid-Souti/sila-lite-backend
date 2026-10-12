"""An .xlsx becomes CSV the rest of the platform can already read.

Every data tool takes .csv/.tsv/.txt. The people who will use the OKR pack
export from RISE, and RISE exports Excel, so the monthly file could not be
opened at all. These are the cases that break a naive reader: a sparse row, a
shared string, a date serial, and Excel's float noise.
"""

import io
import zipfile

import pytest

from sila.core.utils.xlsx import MAX_COLS, XlsxError, read_workbook, workbook_to_csv

MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _workbook(
    sheets: list[tuple[str, str]],
    shared: list[str] | None = None,
    styles: str = "",
    after_data: str = "",
) -> bytes:
    """A minimal but real .xlsx: workbook index, rels, sheets, strings, styles."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        entries = "".join(
            f'<sheet name="{name}" sheetId="{i + 1}" r:id="rId{i + 1}"/>'
            for i, (name, _) in enumerate(sheets)
        )
        zf.writestr(
            "xl/workbook.xml",
            f'<workbook xmlns="{MAIN}" xmlns:r="{REL}"><sheets>{entries}</sheets></workbook>',
        )
        rels = "".join(
            f'<Relationship Id="rId{i + 1}" Target="worksheets/sheet{i + 1}.xml"/>'
            for i in range(len(sheets))
        )
        zf.writestr("xl/_rels/workbook.xml.rels", f"<Relationships>{rels}</Relationships>")
        for i, (_, rows) in enumerate(sheets):
            zf.writestr(
                f"xl/worksheets/sheet{i + 1}.xml",
                f'<worksheet xmlns="{MAIN}"><sheetData>{rows}</sheetData>{after_data}</worksheet>',
            )
        if shared is not None:
            items = "".join(f"<si><t>{s}</t></si>" for s in shared)
            zf.writestr("xl/sharedStrings.xml", f'<sst xmlns="{MAIN}">{items}</sst>')
        if styles:
            zf.writestr("xl/styles.xml", styles)
    return buf.getvalue()


def _row(index: int, cells: str) -> str:
    return f'<row r="{index}">{cells}</row>'


def _num(ref: str, value: str, style: str = "") -> str:
    s = f' s="{style}"' if style else ""
    return f'<c r="{ref}"{s}><v>{value}</v></c>'


# --- the shapes that break a naive reader ------------------------------------


def test_a_gap_mid_row_does_not_shift_the_columns_after_it() -> None:
    """Excel omits empty cells entirely. Reading them in document order puts
    every later value one column to the left — the bug that silently moves
    `Actual` into the `Annual Target` column."""
    data = _workbook([("S", _row(1, _num("A1", "1") + _num("C1", "3")))])
    assert read_workbook(data)[0].rows == [["1", "", "3"]]


def test_a_missing_row_is_filled_so_row_numbers_still_line_up() -> None:
    data = _workbook([("S", _row(1, _num("A1", "1")) + _row(3, _num("A3", "3")))])
    assert read_workbook(data)[0].rows == [["1"], [""], ["3"]]


def test_shared_strings_resolve_to_their_text() -> None:
    cells = '<c r="A1" t="s"><v>1</v></c><c r="B1" t="s"><v>0</v></c>'
    data = _workbook([("S", _row(1, cells))], shared=["Owner", "Objective"])
    assert read_workbook(data)[0].rows == [["Objective", "Owner"]]


def test_excel_float_noise_is_not_shown_to_a_reader() -> None:
    """A cell typed as 7% is stored as 0.070000000000000007."""
    cells = _num("A1", "0.070000000000000007") + _num("B1", "180") + _num("C1", "0.75")
    assert read_workbook(_workbook([("S", _row(1, cells))]))[0].rows == [["0.07", "180", "0.75"]]


def test_a_date_styled_cell_reads_as_a_date_not_a_serial() -> None:
    """46387 is a number until styles.xml says its format is a date."""
    styles = (
        f'<styleSheet xmlns="{MAIN}">'
        '<cellXfs><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>'
    )
    cells = _num("A1", "46387", style="1") + _num("B1", "46387", style="0")
    rows = read_workbook(_workbook([("S", _row(1, cells))], styles=styles))[0].rows
    assert rows == [["2026-12-31", "46387"]]


def test_a_custom_date_format_is_detected_by_its_code() -> None:
    styles = (
        f'<styleSheet xmlns="{MAIN}">'
        '<numFmts><numFmt numFmtId="180" formatCode="dd/mm/yyyy"/></numFmts>'
        '<cellXfs><xf numFmtId="180"/></cellXfs></styleSheet>'
    )
    rows = read_workbook(_workbook([("S", _row(1, _num("A1", "46387", "0")))], styles=styles))[
        0
    ].rows
    assert rows == [["2026-12-31"]]


def test_a_colour_block_in_a_format_string_is_not_mistaken_for_a_date() -> None:
    """'[Red]0.00' contains a 'd'. Escaped literals must come out first."""
    styles = (
        f'<styleSheet xmlns="{MAIN}">'
        '<numFmts><numFmt numFmtId="181" formatCode="[Red]0.00"/></numFmts>'
        '<cellXfs><xf numFmtId="181"/></cellXfs></styleSheet>'
    )
    rows = read_workbook(_workbook([("S", _row(1, _num("A1", "142", "0")))], styles=styles))[0].rows
    assert rows == [["142"]]


def test_a_formula_returns_its_cached_value_and_the_sheet_says_so() -> None:
    cells = '<c r="A1"><f>SUM(B1:B9)</f><v>0.763944</v></c>'
    sheet = read_workbook(_workbook([("S", _row(1, cells))]))[0]
    assert sheet.rows == [["0.763944"]]
    assert sheet.has_formulas, "a caller needs to know the values are cached"


def test_an_error_cell_is_shown_rather_than_swallowed() -> None:
    cells = '<c r="A1" t="e"><v>#DIV/0!</v></c>'
    assert read_workbook(_workbook([("S", _row(1, cells))]))[0].rows == [["#DIV/0!"]]


def test_booleans_and_inline_strings_read_correctly() -> None:
    cells = '<c r="A1" t="b"><v>1</v></c><c r="B1" t="inlineStr"><is><t>Milestone</t></is></c>'
    assert read_workbook(_workbook([("S", _row(1, cells))]))[0].rows == [["TRUE", "Milestone"]]


def test_a_two_letter_column_lands_in_the_right_place() -> None:
    """Base-26 done wrong puts AA at index 0 or 27 instead of 26."""
    data = _workbook([("S", _row(1, _num("A1", "1") + _num("AA1", "27")))])
    row = read_workbook(data)[0].rows[0]
    assert len(row) == 27 and row[0] == "1" and row[26] == "27"


def test_a_runaway_column_reference_is_ignored_rather_than_allocated() -> None:
    data = _workbook([("S", _row(1, _num("A1", "1") + _num("XFD1", "2")))])
    assert len(read_workbook(data)[0].rows[0]) <= MAX_COLS


# --- picking the right sheet --------------------------------------------------


def test_the_first_sheet_with_content_wins_over_an_empty_cover_tab() -> None:
    data = _workbook([("Cover", ""), ("OKR Data", _row(1, _num("A1", "1")))])
    name, csv_text = workbook_to_csv(data)
    assert name == "OKR Data" and csv_text.strip() == "1"


def test_a_named_sheet_can_be_asked_for_case_insensitively() -> None:
    data = _workbook(
        [("OKR Data", _row(1, _num("A1", "1"))), ("POC Notes", _row(1, _num("A1", "2")))]
    )
    assert workbook_to_csv(data, sheet="poc notes")[1].strip() == "2"


def test_asking_for_a_sheet_that_is_not_there_names_the_ones_that_are() -> None:
    data = _workbook([("OKR Data", _row(1, _num("A1", "1")))])
    with pytest.raises(XlsxError, match="OKR Data"):
        workbook_to_csv(data, sheet="Nope")


def test_sheets_come_back_in_workbook_order_not_zip_order() -> None:
    data = _workbook([("First", _row(1, _num("A1", "1"))), ("Second", _row(1, _num("A1", "2")))])
    assert [s.name for s in read_workbook(data)] == ["First", "Second"]


# --- refusals -----------------------------------------------------------------


def test_an_old_xls_is_refused_with_advice_rather_than_a_zip_error() -> None:
    with pytest.raises(XlsxError, match="re-save it as .xlsx"):
        read_workbook(b"\xd0\xcf\x11\xe0not a zip")


def test_an_empty_file_is_refused() -> None:
    with pytest.raises(XlsxError, match="empty"):
        read_workbook(b"")


def test_a_zip_that_is_not_a_workbook_is_refused() -> None:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("hello.txt", "not a workbook")
    with pytest.raises(XlsxError):
        read_workbook(buf.getvalue())


def test_csv_quotes_a_value_containing_a_comma() -> None:
    cells = '<c r="A1" t="inlineStr"><is><t>Deliver the roadmap: design, build, launch</t></is></c>'
    assert (
        '"Deliver the roadmap: design, build, launch"'
        in read_workbook(_workbook([("S", _row(1, cells))]))[0].to_csv()
    )


def _str(ref: str, index: int) -> str:
    return f'<c r="{ref}" t="s"><v>{index}</v></c>'


def test_a_carriage_return_escaped_by_excel_comes_back_as_one() -> None:
    """Excel stores CR as _x000D_ and a literal "_x" as _x005F_x…"""
    data = _workbook(
        [("S", _row(1, _str("A1", 0) + _str("B1", 1)))],
        shared=["line one_x000D_\nline two", "keep _x005F_x000D_ literally"],
    )
    rows = read_workbook(data)[0].rows
    assert rows[0][0] == "line one\r\nline two"
    assert rows[0][1] == "keep _x000D_ literally"


def test_a_merged_range_shows_its_value_in_every_cell_only_when_asked() -> None:
    rows = (
        _row(1, _str("A1", 0) + _str("B1", 1)) + _row(2, _num("B2", "2")) + _row(3, _num("B3", "3"))
    )
    data = _workbook(
        [("S", rows)],
        shared=["Yes", "x"],
        after_data='<mergeCells count="1"><mergeCell ref="A1:A3"/></mergeCells>',
    )
    assert [r[0] for r in read_workbook(data)[0].rows] == ["Yes", "", ""]
    filled = read_workbook(data, fill_merged=True)[0].rows
    assert [r[0] for r in filled] == ["Yes", "Yes", "Yes"]


def test_a_merge_to_the_sheet_edge_is_clipped_to_the_rows_that_exist() -> None:
    data = _workbook(
        [("S", _row(1, _str("A1", 0)) + _row(2, _num("B2", "1")))],
        shared=["T"],
        after_data='<mergeCells count="1"><mergeCell ref="A1:XFD1048576"/></mergeCells>',
    )
    rows = read_workbook(data, fill_merged=True)[0].rows
    assert len(rows) == 2 and len(rows[0]) == 2
