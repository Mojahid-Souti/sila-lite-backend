"""Timestamps as the browser must read them.

Columns declared `timestamp without time zone` hold UTC (the database runs in
UTC and `now()` is stored as such). `isoformat()` on those naive values drops
the zone, and a browser parses a zone-less ISO string as LOCAL time — the bell
said "4 hours ago" for a minute-old entry in Muscat. Always say it is UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime


def utc_iso(value: datetime) -> str:
    """An ISO-8601 string with an explicit UTC offset (naive values are UTC)."""
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).isoformat()
