"""Slides Engine v2 — the closed vocabularies (layouts, palettes,
container types), with zero internal dependencies so every other slides
module (schema, geometry, reflow, render, tokens) can import them without
creating an import cycle.
"""

from __future__ import annotations

from enum import StrEnum


class LayoutType(StrEnum):
    TITLE_HERO = "title-hero"
    SPLIT_GRID = "split-grid"
    THREE_COLUMN_CARDS = "3-column-cards"
    METRIC_SHOWCASE = "metric-showcase"
    QUOTE_FOCUS = "quote-focus"
    DATA_SHOWCASE = "data-showcase"  # one big table or chart + an optional caption
    PROCESS_FLOW = "process-flow"  # 2–5 numbered steps joined by arrows
    COMPARISON = "comparison"  # exactly two options, side by side (A vs B)
    TIMELINE = "timeline"  # 2–6 milestones along a horizontal line
    IMAGE_FEATURE = "image-feature"  # one generated hero image + a title/caption


class Palette(StrEnum):
    MODERN_SLATE = "modern-slate"
    MINIMAL_EDITORIAL = "minimal-editorial"
    HIGH_CONTRAST_BOLD = "high-contrast-bold"
    MIDNIGHT_TEAL = "midnight-teal"
    WARM_SAND = "warm-sand"
    ROYAL_INDIGO = "royal-indigo"


class ContainerType(StrEnum):
    CARD = "card"
    METRIC = "metric"
    HERO_TEXT = "hero-text"
    TABLE = "table"  # rows[][] → a native PowerPoint table (editable)
    CHART = "chart"  # a chart spec → a native embedded chart (editable)
    IMAGE = "image"  # image_prompt → a locally-generated raster picture


# A table/chart is a "figure": it is only valid on the DATA_SHOWCASE layout,
# and DATA_SHOWCASE must hold exactly one of them (see schema validation).
FIGURE_CONTAINERS = frozenset({ContainerType.TABLE, ContainerType.CHART})


class ChartKind(StrEnum):
    BAR = "bar"  # clustered columns
    LINE = "line"
    PIE = "pie"


class IconName(StrEnum):
    """The closed icon vocabulary. The model may name one of these per
    container; code draws it as a native vector shape (preset geometry or
    a simple custom path). An unknown name is rejected by the schema."""

    CHECK = "check"
    BOLT = "bolt"
    GEAR = "gear"
    DATABASE = "database"
    DOCUMENT = "document"
    STAR = "star"
    ARROW = "arrow"
    SHIELD = "shield"
    CHART = "chart"
    PLUS = "plus"
    CLOUD = "cloud"
    IDEA = "idea"
    TARGET = "target"
    PEOPLE = "people"
    LOCK = "lock"
    DIAMOND = "diamond"
    HEXAGON = "hexagon"
