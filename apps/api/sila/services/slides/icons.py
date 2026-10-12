"""Slides Engine v2 — the icon library (native vector, editable).

Each icon is described as one or more PARTS, drawn in a 0..1 unit square
that the renderer scales to the target EMU box. A part is either a
PowerPoint preset geometry (always renders, zero risk) or a simple custom
path in a 100×100 coordinate space. Icons are monochrome — the renderer
passes the palette color — so an icon can never clash with the deck, the
same rule every layout follows.

Complexity: O(parts) per icon; pure data + string assembly.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from sila.services.slides.enums import IconName


@dataclass(frozen=True)
class IconPart:
    kind: str  # "preset" | "path"
    spec: str  # preset name, or path-command XML (100×100 space)
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 1.0, 1.0)  # rx, ry, rw, rh
    mode: str = "fill"  # "fill" | "stroke"


@dataclass(frozen=True)
class IconDef:
    parts: tuple[IconPart, ...] = field(default_factory=tuple)


def _p(*pts: tuple[float, float]) -> str:
    return "".join(f'<a:pt x="{int(x)}" y="{int(y)}"/>' for x, y in pts)


def _move(x: float, y: float) -> str:
    return f"<a:moveTo>{_p((x, y))}</a:moveTo>"


def _line(x: float, y: float) -> str:
    return f"<a:lnTo>{_p((x, y))}</a:lnTo>"


def _cubic(p1: tuple[float, float], p2: tuple[float, float], p3: tuple[float, float]) -> str:
    return f"<a:cubicBezTo>{_p(p1, p2, p3)}</a:cubicBezTo>"


_CLOSE = "<a:close/>"

# --- custom paths (100×100 space) -------------------------------------------

_CHECK = _move(18, 52) + _line(40, 74) + _line(84, 26)  # open polyline → stroked
_SHIELD = (
    _move(50, 6)
    + _line(88, 20)
    + _line(88, 48)
    + _cubic((88, 74), (72, 90), (50, 96))
    + _cubic((28, 90), (12, 74), (12, 48))
    + _line(12, 20)
    + _CLOSE
)
_PERSON_BODY = (
    _move(14, 100)
    + _cubic((14, 74), (32, 62), (50, 62))
    + _cubic((68, 62), (86, 74), (86, 100))
    + _CLOSE
)
_SHACKLE = (  # an open inverted-U for the lock
    _move(30, 48) + _line(30, 34) + _cubic((30, 18), (70, 18), (70, 34)) + _line(70, 48)
)

_ICONS: dict[IconName, IconDef] = {
    IconName.CHECK: IconDef((IconPart("path", _CHECK, mode="stroke"),)),
    IconName.BOLT: IconDef((IconPart("preset", "lightningBolt"),)),
    IconName.GEAR: IconDef((IconPart("preset", "gear6"),)),
    IconName.DATABASE: IconDef((IconPart("preset", "can"),)),
    IconName.DOCUMENT: IconDef((IconPart("preset", "flowChartDocument"),)),
    IconName.STAR: IconDef((IconPart("preset", "star5"),)),
    IconName.ARROW: IconDef((IconPart("preset", "rightArrow", (0.0, 0.2, 1.0, 0.6)),)),
    IconName.SHIELD: IconDef((IconPart("path", _SHIELD),)),
    IconName.CHART: IconDef(
        (
            IconPart("preset", "rect", (0.06, 0.46, 0.22, 0.5)),
            IconPart("preset", "rect", (0.39, 0.26, 0.22, 0.7)),
            IconPart("preset", "rect", (0.72, 0.06, 0.22, 0.9)),
        )
    ),
    IconName.PLUS: IconDef((IconPart("preset", "mathPlus", (0.1, 0.1, 0.8, 0.8)),)),
    IconName.CLOUD: IconDef((IconPart("preset", "cloud", (0.0, 0.15, 1.0, 0.7)),)),
    IconName.IDEA: IconDef((IconPart("preset", "sun"),)),
    IconName.TARGET: IconDef(
        (
            IconPart("preset", "donut"),
            IconPart("preset", "ellipse", (0.36, 0.36, 0.28, 0.28)),
        )
    ),
    IconName.PEOPLE: IconDef(
        (
            IconPart("preset", "ellipse", (0.32, 0.04, 0.36, 0.36)),
            IconPart("path", _PERSON_BODY, (0.0, 0.0, 1.0, 1.0)),
        )
    ),
    IconName.LOCK: IconDef(
        (
            IconPart("path", _SHACKLE, (0.0, 0.0, 1.0, 1.0), mode="stroke"),
            IconPart("preset", "roundRect", (0.16, 0.46, 0.68, 0.5)),
        )
    ),
    IconName.DIAMOND: IconDef((IconPart("preset", "diamond"),)),
    IconName.HEXAGON: IconDef((IconPart("preset", "hexagon"),)),
}


def icon_def(name: IconName) -> IconDef:
    return _ICONS[name]
