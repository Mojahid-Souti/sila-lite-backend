"""Read an .xlsx workbook into rows, using only the standard library.

Every data tool in the platform consumes CSV — `.csv`, `.tsv`, `.txt` and
nothing else. The people who will actually use the OKR pack export from RISE,
and RISE exports Excel, so without this the monthly file cannot be opened at
all. Converting once at the boundary means every existing tool (profiling,
cleaning, charts, the deck) keeps working unchanged.

Why no openpyxl: an .xlsx is a zip of XML, and the part we need — the used
range of a sheet, with shared strings resolved and dates formatted — is a small
read. A dependency would earn its place if we had to WRITE workbooks (styles,
number formats, column widths); it does not for reading.

What this deliberately does NOT do: formulas are not evaluated. Excel caches
the last computed value next to the formula, and that cached value is what we
return — which is right for a file exported from a system, and wrong for one
where someone edited an input without opening Excel. `Sheet.has_formulas` says
whether that risk applies so a caller can warn.

Complexity: O(cells) time over the used range, O(shared strings + row) space —
one row is materialised at a time, the shared-string table once.
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import re
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

# Excel's epoch. Day 1 is 1900-01-01, but Excel believes 1900 was a leap year,
# so serials from 60 onward are one day ahead of reality — the standard fix is
# to anchor at 1899-12-30 and add the serial.
_EXCEL_EPOCH = dt.datetime(1899, 12, 30)

# A cell is a date only because its number format says so. These are the
# built-in format ids Excel reserves for dates and times; anything custom is
# detected by the format string itself.
_BUILTIN_DATE_FORMATS = frozenset(
    {14, 15, 16, 17, 18, 19, 20, 21, 22, 45, 46, 47, 27, 30, 36, 50, 57}
)
_DATE_TOKENS = re.compile(r"[dmyhs]", re.I)
# Escaped literals and colour/condition blocks inside a format string can
# contain d/m/y by accident ("[Red]", "\\d"), so they come out before the test.
_FORMAT_NOISE = re.compile(r"\[[^\]]*\]|\"[^\"]*\"|\\.")

_CELL_REF = re.compile(r"([A-Z]+)(\d+)")

# A workbook with a runaway used-range would otherwise become a multi-GB CSV.
# Any real OKR sheet is a few hundred rows; this is a guard, not a target.
MAX_ROWS = 100_000
MAX_COLS = 1_024


class XlsxError(ValueError):
    """The bytes are not a readable .xlsx workbook."""


@dataclass
class Sheet:
    """One worksheet's used range, as rows of already-stringified cells."""

    name: str
    rows: list[list[str]] = field(default_factory=list)
    has_formulas: bool = False

    @property
    def is_empty(self) -> bool:
        return not any(any(cell for cell in row) for row in self.rows)

    def to_csv(self) -> str:
        """The sheet as CSV text, ready for every existing data tool."""
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\n")
        writer.writerows(self.rows)
        return buf.getvalue()


def _column_index(ref: str) -> int:
    """'A' -> 0, 'Z' -> 25, 'AA' -> 26. Bijective base-26, not plain base-26."""
    n = 0
    for char in ref:
        n = n * 26 + (ord(char) - 64)
    return n - 1


def _date_format_ids(zf: zipfile.ZipFile) -> frozenset[int]:
    """Style indices whose number format renders a date.

    Excel stores a format ID per style and the style index per cell, so telling
    a date from the number 46387 means walking styles.xml. Missing or malformed
    styles are not fatal — the numbers simply stay numbers.
    """
    try:
        root = ET.fromstring(zf.read("xl/styles.xml"))
    except (KeyError, ET.ParseError):
        return frozenset()

    custom: set[int] = set()
    for fmt in root.iter(f"{_MAIN}numFmt"):
        code = _FORMAT_NOISE.sub("", fmt.get("formatCode") or "")
        if _DATE_TOKENS.search(code):
            try:
                custom.add(int(fmt.get("numFmtId") or -1))
            except ValueError:
                continue

    dated: set[int] = set()
    cell_formats = root.find(f"{_MAIN}cellXfs")
    for index, xf in enumerate(cell_formats if cell_formats is not None else []):
        try:
            fmt_id = int(xf.get("numFmtId") or 0)
        except ValueError:
            continue
        if fmt_id in _BUILTIN_DATE_FORMATS or fmt_id in custom:
            dated.add(index)
    return frozenset(dated)


# Excel escapes characters XML cannot carry as _xHHHH_ — a carriage return in a
# cell is stored as "_x000D_" — and a literal "_x" sequence as "_x005F_x…". One
# left-to-right pass decodes both correctly.
_ESCAPE = re.compile(r"_x([0-9A-Fa-f]{4})_")


def _unescape(text: str) -> str:
    return _ESCAPE.sub(lambda m: chr(int(m.group(1), 16)), text)


def _shared_strings(zf: zipfile.ZipFile) -> list[str]:
    """The workbook's string table. Runs inside one entry are concatenated, so
    a cell styled mid-word ("Q1 **2026**") comes back as one string."""
    try:
        root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
    except (KeyError, ET.ParseError):
        return []
    return [
        _unescape("".join(t.text or "" for t in si.iter(f"{_MAIN}t")))
        for si in root.findall(f"{_MAIN}si")
    ]


def _serial_to_text(serial: float) -> str:
    """An Excel date serial as ISO text. Whole days lose the midnight suffix."""
    moment = _EXCEL_EPOCH + dt.timedelta(days=serial)
    if moment.hour or moment.minute or moment.second:
        return moment.strftime("%Y-%m-%d %H:%M:%S")
    return moment.strftime("%Y-%m-%d")


def _number_to_text(raw: str) -> str:
    """A stored number as the shortest faithful text.

    Excel writes 0.070000000000000007 for a cell a human typed as 7%. Passing
    that straight through puts float noise in front of a reader, so values are
    rounded to a precision no spreadsheet exceeds meaningfully (10 decimals) and
    trailing zeros are dropped. Integers lose the ".0" entirely.
    """
    try:
        value = float(raw)
    except ValueError:
        return raw
    if value == int(value) and abs(value) < 1e15:
        return str(int(value))
    return f"{round(value, 10):f}".rstrip("0").rstrip(".")


def _cell_text(cell: ET.Element, shared: list[str], date_styles: frozenset[int]) -> str:
    """One cell's displayed value as text."""
    kind = cell.get("t")

    if kind == "inlineStr":
        return _unescape("".join(t.text or "" for t in cell.iter(f"{_MAIN}t")))

    value = cell.find(f"{_MAIN}v")
    if value is None or value.text is None:
        return ""
    raw = value.text

    if kind == "s":
        try:
            return shared[int(raw)]
        except (ValueError, IndexError):
            return ""
    if kind in ("str", "e"):
        # "str" is a formula's cached string; "e" is an error like #DIV/0!,
        # which is shown rather than swallowed so a broken sheet is visible.
        return _unescape(raw)
    if kind == "b":
        return "TRUE" if raw == "1" else "FALSE"

    try:
        style = int(cell.get("s") or -1)
    except ValueError:
        style = -1
    if style in date_styles:
        try:
            return _serial_to_text(float(raw))
        except (ValueError, OverflowError):
            return raw
    return _number_to_text(raw)


def _sheet_parts(zf: zipfile.ZipFile) -> list[tuple[str, str]]:
    """(sheet name, zip part) in workbook order.

    The order matters: "the first sheet" is what a person means by "the data",
    and workbook.xml is the only place that order is recorded.
    """
    try:
        workbook = ET.fromstring(zf.read("xl/workbook.xml"))
        rels_xml = ET.fromstring(zf.read("xl/_rels/workbook.xml.rels"))
    except (KeyError, ET.ParseError) as exc:
        raise XlsxError("workbook is missing its index") from exc

    targets = {rel.get("Id"): (rel.get("Target") or "") for rel in rels_xml}
    parts: list[tuple[str, str]] = []
    for sheet in workbook.iter(f"{_MAIN}sheet"):
        target = targets.get(sheet.get(f"{_REL}id") or "")
        if not target:
            continue
        # Targets are relative to xl/ and may be written "/xl/worksheets/…".
        part = target.lstrip("/")
        if not part.startswith("xl/"):
            part = f"xl/{part}"
        if part in zf.namelist():
            parts.append((sheet.get("name") or "Sheet", part))
    return parts


def _fill_merged(root: ET.Element, rows: dict[int, dict[int, str]], widest: int) -> None:
    """Give every cell of a merged range the value its top-left cell holds.

    A merged cell shows one value across its range, but only the top-left cell
    stores it. Clipped to the used range, so a merge running to the sheet's
    edge costs no more than the rows that exist.
    Complexity: O(cells inside merged ranges, within the used range).
    """
    last_row = max(rows) if rows else 0
    for merge in root.iter(f"{_MAIN}mergeCell"):
        corners = [_CELL_REF.match(part) for part in (merge.get("ref") or "").split(":")]
        if len(corners) != 2 or corners[0] is None or corners[1] is None:
            continue
        top, left = int(corners[0].group(2)), _column_index(corners[0].group(1))
        bottom, right = int(corners[1].group(2)), _column_index(corners[1].group(1))
        value = rows.get(top, {}).get(left)
        if not value:
            continue
        for row_index in range(top, min(bottom, last_row) + 1):
            cells = rows.setdefault(row_index, {})
            for column in range(left, min(right, widest - 1) + 1):
                cells.setdefault(column, value)


def _read_sheet(
    zf: zipfile.ZipFile,
    name: str,
    part: str,
    shared: list[str],
    dates: frozenset[int],
    fill_merged: bool = False,
) -> Sheet:
    """One worksheet as a rectangle of text.

    Sparse by nature: Excel omits empty cells and whole empty rows, so cells are
    placed by their reference and the gaps filled, otherwise a blank cell in the
    middle of a row silently shifts every value after it one column left.
    """
    sheet = Sheet(name=name)
    try:
        root = ET.fromstring(zf.read(part))
    except ET.ParseError as exc:
        raise XlsxError(f"sheet {name!r} is not readable") from exc

    rows: dict[int, dict[int, str]] = {}
    widest = 0
    for row in root.iter(f"{_MAIN}row"):
        try:
            row_index = int(row.get("r") or 0)
        except ValueError:
            continue
        if row_index <= 0 or row_index > MAX_ROWS:
            continue
        cells: dict[int, str] = {}
        for cell in row.findall(f"{_MAIN}c"):
            match = _CELL_REF.match(cell.get("r") or "")
            if match is None:
                continue
            column = _column_index(match.group(1))
            if column >= MAX_COLS:
                continue
            if cell.find(f"{_MAIN}f") is not None:
                sheet.has_formulas = True
            text = _cell_text(cell, shared, dates)
            if text:
                cells[column] = text
        if cells:
            rows[row_index] = cells
            widest = max(widest, max(cells) + 1)

    if not rows:
        return sheet
    if fill_merged:
        _fill_merged(root, rows, widest)
    # Trailing blank rows carry no information; interior ones do, so the range
    # runs to the last row that had content.
    for row_index in range(1, max(rows) + 1):
        cells = rows.get(row_index, {})
        sheet.rows.append([cells.get(column, "") for column in range(widest)])
    return sheet


def read_workbook(data: bytes, *, fill_merged: bool = False) -> list[Sheet]:
    """Every sheet in an .xlsx, in workbook order.

    `fill_merged` gives every cell of a merged range its displayed value; off by
    default, because a merged header over two columns would otherwise name both.

    Raises XlsxError when the bytes are not a workbook — including the common
    case of a .xls (the old binary format), which is not a zip at all.
    """
    if not data:
        raise XlsxError("the file is empty")
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise XlsxError(
            "not an .xlsx workbook — if this is an older .xls, re-save it as .xlsx"
        ) from exc

    with zf:
        parts = _sheet_parts(zf)
        if not parts:
            raise XlsxError("the workbook has no worksheets")
        shared = _shared_strings(zf)
        dates = _date_format_ids(zf)
        return [_read_sheet(zf, name, part, shared, dates, fill_merged) for name, part in parts]


def workbook_to_csv(
    data: bytes, sheet: str | None = None, *, fill_merged: bool = False
) -> tuple[str, str]:
    """An .xlsx as (sheet name, CSV text).

    Picks the named sheet when asked, otherwise the first sheet that has any
    content — a workbook whose first tab is a cover page or a notes sheet is
    common enough that "the first sheet" would pick the wrong one.
    """
    sheets = read_workbook(data, fill_merged=fill_merged)
    if sheet is not None:
        wanted = sheet.strip().casefold()
        for candidate in sheets:
            if candidate.name.strip().casefold() == wanted:
                return candidate.name, candidate.to_csv()
        names = ", ".join(repr(s.name) for s in sheets)
        raise XlsxError(f"no sheet named {sheet!r} — the workbook has {names}")

    for candidate in sheets:
        if not candidate.is_empty:
            return candidate.name, candidate.to_csv()
    return sheets[0].name, sheets[0].to_csv()
