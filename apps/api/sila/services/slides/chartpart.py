"""Slides Engine v2 — native embedded chart parts (DrawingML charts).

A validated ChartSpec → a real PowerPoint chart part (``c:chartSpace``)
that renders from CACHED values (``c:numCache``/``c:strCache``) with no
embedded workbook. That keeps the whole engine stdlib-only and
byte-stable; the trade-off is that "Edit Data" in PowerPoint has no
backing spreadsheet to open — the chart still displays and restyles
natively (double-click to change type/colors), which is what a pitch
deck needs. An embedded xlsx can be added later behind the same seam.

Colors come only from the palette tokens (deterministic tints of the
accent) — a chart never invents a color, same rule as every layout.

Complexity: O(categories × series) string assembly.
"""

from __future__ import annotations

from xml.sax.saxutils import escape

from sila.services.slides.enums import ChartKind
from sila.services.slides.schema import ChartSpec
from sila.services.slides.tokens import PaletteTokens

_C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
_A = "http://schemas.openxmlformats.org/drawingml/2006/main"
_R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    return int(color[0:2], 16), int(color[2:4], 16), int(color[4:6], 16)


def _blend(color: str, target: str, ratio: float) -> str:
    """Move `color` `ratio` of the way toward `target` (0..1). Deterministic."""
    a, b = _hex_to_rgb(color), _hex_to_rgb(target)
    mixed = tuple(round(x + (y - x) * ratio) for x, y in zip(a, b, strict=True))
    return "".join(f"{max(0, min(255, c)):02X}" for c in mixed)


def swatches(tokens: PaletteTokens, n: int) -> list[str]:
    """n distinct chart colors derived from the accent — alternating tints
    toward the light surface and shades toward the text, so adjacent
    slices/series stay legible. Deterministic and palette-bound."""
    if n <= 1:
        return [tokens.accent]
    ratios = [0.0, 0.30, -0.28, 0.52, -0.48, 0.16, 0.68, -0.16]
    out: list[str] = []
    for i in range(n):
        r = ratios[i % len(ratios)]
        toward = tokens.bg if r >= 0 else tokens.text
        out.append(tokens.accent if r == 0 else _blend(tokens.accent, toward, abs(r)))
    return out


def _str_cache(values: list[str]) -> str:
    pts = "".join(f'<c:pt idx="{i}"><c:v>{escape(v)}</c:v></c:pt>' for i, v in enumerate(values))
    return f'<c:strCache><c:ptCount val="{len(values)}"/>{pts}</c:strCache>'


def _num_cache(values: list[float]) -> str:
    pts = "".join(f'<c:pt idx="{i}"><c:v>{_fmt_num(v)}</c:v></c:pt>' for i, v in enumerate(values))
    return f'<c:numCache><c:formatCode>General</c:formatCode><c:ptCount val="{len(values)}"/>{pts}</c:numCache>'  # noqa: E501


def _fmt_num(value: float) -> str:
    return str(int(value)) if value == int(value) else repr(value)


def _cat_ref(categories: list[str]) -> str:
    return f"<c:cat><c:strRef><c:f>Sheet1!$A$2:$A${len(categories) + 1}</c:f>{_str_cache(categories)}</c:strRef></c:cat>"  # noqa: E501


def _val_ref(col: int, values: list[float]) -> str:
    letter = chr(ord("B") + col)
    ref = f"Sheet1!${letter}$2:${letter}${len(values) + 1}"
    return f"<c:val><c:numRef><c:f>{ref}</c:f>{_num_cache(values)}</c:numRef></c:val>"


def _series_tx(col: int, name: str) -> str:
    letter = chr(ord("B") + col)
    return (
        f"<c:tx><c:strRef><c:f>Sheet1!${letter}$1</c:f>"
        f'<c:strCache><c:ptCount val="1"/><c:pt idx="0"><c:v>{escape(name)}</c:v></c:pt>'
        "</c:strCache></c:strRef></c:tx>"
    )


def _solid(color: str) -> str:
    return f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill>'


def _bar_or_line_ser(
    idx: int, name: str, values: list[float], cats: list[str], color: str, *, line: bool
) -> str:
    if line:
        shape = f'<c:spPr><a:ln w="28575">{_solid(color)}</a:ln></c:spPr><c:marker><c:symbol val="none"/></c:marker>'  # noqa: E501
    else:
        shape = f"<c:spPr>{_solid(color)}</c:spPr>"
    return (
        f'<c:ser><c:idx val="{idx}"/><c:order val="{idx}"/>'
        f"{_series_tx(idx, name or f'Series {idx + 1}')}{shape}"
        f"{_cat_ref(cats)}{_val_ref(idx, values)}</c:ser>"
    )


def _pie_ser(name: str, values: list[float], cats: list[str], colors: list[str]) -> str:
    d_pts = "".join(
        f'<c:dPt><c:idx val="{i}"/><c:bubble3D val="0"/><c:spPr>{_solid(colors[i % len(colors)])}</c:spPr></c:dPt>'  # noqa: E501
        for i in range(len(cats))
    )
    return (
        f'<c:ser><c:idx val="0"/><c:order val="0"/>{_series_tx(0, name or "Series 1")}'
        f"{d_pts}{_cat_ref(cats)}{_val_ref(0, values)}</c:ser>"
    )


def _axes(cat_color: str) -> str:
    txt = f'<c:txPr><a:bodyPr/><a:lstStyle/><a:p><a:pPr><a:defRPr sz="1000">{_solid(cat_color)}</a:defRPr></a:pPr><a:endParaRPr lang="en-US"/></a:p></c:txPr>'  # noqa: E501
    return (
        '<c:catAx><c:axId val="1"/><c:scaling><c:orientation val="minMax"/></c:scaling>'
        '<c:delete val="0"/><c:axPos val="b"/>'
        f'{txt}<c:crossAx val="2"/></c:catAx>'
        '<c:valAx><c:axId val="2"/><c:scaling><c:orientation val="minMax"/></c:scaling>'
        '<c:delete val="0"/><c:axPos val="l"/>'
        '<c:majorGridlines><c:spPr><a:ln w="6350"><a:solidFill>'
        f'<a:srgbClr val="{cat_color}"><a:alpha val="18000"/></a:srgbClr>'
        "</a:solidFill></a:ln></c:spPr></c:majorGridlines>"
        f'{txt}<c:crossAx val="1"/></c:valAx>'
    )


def build_chart_xml(spec: ChartSpec, tokens: PaletteTokens) -> str:
    """A validated ChartSpec → a complete chart part. Deterministic."""
    cats = spec.categories
    if spec.kind is ChartKind.PIE:
        colors = swatches(tokens, len(cats))
        series = spec.series[0] if spec.series else None
        vals = series.values if series else [0.0] * len(cats)
        plot = (
            '<c:pieChart><c:varyColors val="1"/>'
            f"{_pie_ser(series.name if series else '', vals, cats, colors)}"
            '<c:firstSliceAng val="0"/></c:pieChart>'
        )
        legend = '<c:legend><c:legendPos val="r"/><c:overlay val="0"/></c:legend>'
    else:
        line = spec.kind is ChartKind.LINE
        colors = swatches(tokens, len(spec.series))
        sers = "".join(
            _bar_or_line_ser(i, s.name, s.values, cats, colors[i], line=line)
            for i, s in enumerate(spec.series)
        )
        tag = "lineChart" if line else "barChart"
        head = "" if line else '<c:barDir val="col"/><c:grouping val="clustered"/>'
        grouping = '<c:grouping val="standard"/>' if line else ""
        plot = (
            f'<c:{tag}>{head}{grouping}<c:varyColors val="0"/>{sers}'
            '<c:axId val="1"/><c:axId val="2"/>'
            f"</c:{tag}>{_axes(tokens.muted)}"
        )
        legend = (
            '<c:legend><c:legendPos val="b"/><c:overlay val="0"/></c:legend>'
            if len(spec.series) > 1
            else ""
        )

    label_txt = (
        f'<c:txPr><a:bodyPr/><a:lstStyle/><a:p><a:pPr><a:defRPr sz="1000">'
        f'{_solid(tokens.muted)}</a:defRPr></a:pPr><a:endParaRPr lang="en-US"/></a:p></c:txPr>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<c:chartSpace xmlns:c="{_C}" xmlns:a="{_A}" xmlns:r="{_R}">'
        '<c:roundedCorners val="0"/><c:chart>'
        '<c:autoTitleDeleted val="1"/>'
        f"<c:plotArea><c:layout/>{plot}<c:spPr><a:noFill/><a:ln><a:noFill/></a:ln></c:spPr></c:plotArea>"
        f'{legend}<c:plotVisOnly val="1"/><c:dispBlanksAs val="gap"/></c:chart>'
        f"<c:spPr><a:noFill/><a:ln><a:noFill/></a:ln></c:spPr>{label_txt}"
        "</c:chartSpace>"
    )
