"""PowerPoint's own identity fields, refreshed when a slide is cloned.

Both deck builders work by cloning a designed slide and substituting its
contents. A verbatim copy brings PowerPoint's identity fields with it:

* `p14:creationId` identifies a SLIDE
* `a16:creationId` identifies a SHAPE
* `p14:modId` marks a modification, used for merge and compare
* `a16:colId` / `a16:rowId` identify a COLUMN and a ROW inside a table

Two slides claiming one identity are two slides PowerPoint cannot tell apart,
and it renders them blank. This cost five rounds of debugging on the OKR deck
because nothing downstream can see it: the fields live in Microsoft EXTENSION
namespaces, so the XSD does not validate them, LibreOffice ignores them
entirely, and the PDF renders perfectly either way.

Values are DERIVED rather than random so a rebuild of the same input still
produces the same bytes — both builders document reproducible output, and a
random GUID per run would quietly take that away.

Complexity: O(size of the part).
"""

from __future__ import annotations

import hashlib
import itertools
import re
from collections.abc import Callable

# The element name is matched with ANY prefix: which one a serialiser happened
# to write is not something this should depend on, and one of these builders
# renames prefixes on its round-trip.
P14_CREATION_ID = re.compile(r'(<(?:[A-Za-z0-9_.-]+:)?creationId\b[^>]*?\bval=")(\d+)(")')
A16_CREATION_ID = re.compile(r'(<(?:[A-Za-z0-9_.-]+:)?creationId\b[^>]*?\bid=")(\{[^"]*\})(")')
P14_MOD_ID = re.compile(r'(<(?:[A-Za-z0-9_.-]+:)?modId\b[^>]*?\bval=")(\d+)(")')
# Table geometry ids. A cloned table gave all four of its <a:gridCol> elements
# the same colId, for the same reason the shapes shared a creationId.
A16_COL_ID = re.compile(r'(<(?:[A-Za-z0-9_.-]+:)?colId\b[^>]*?\bval=")([^"]*)(")')
A16_ROW_ID = re.compile(r'(<(?:[A-Za-z0-9_.-]+:)?rowId\b[^>]*?\bval=")([^"]*)(")')


def _derive(kind: str, slide_number: int, index: int, original: str) -> bytes:
    seed = f"sila-slides/{kind}/{slide_number}/{index}/{original}".encode()
    return hashlib.blake2b(seed, digest_size=16).digest()


def _fresh_uint32(kind: str, slide_number: int, index: int, original: str) -> str:
    # 0 is not a plausible creation id; nudge it rather than risk the edge.
    value = int.from_bytes(_derive(kind, slide_number, index, original)[:4], "big")
    return str(value or 1)


def _fresh_guid(slide_number: int, index: int, original: str) -> str:
    raw = bytearray(_derive("a16", slide_number, index, original))
    raw[6] = (raw[6] & 0x0F) | 0x40  # version 4
    raw[8] = (raw[8] & 0x3F) | 0x80  # RFC-4122 variant
    hexed = raw.hex().upper()
    return f"{{{hexed[:8]}-{hexed[8:12]}-{hexed[12:16]}-{hexed[16:20]}-{hexed[20:32]}}}"


def refresh_identity(xml: str, slide_number: int) -> str:
    """Give one cloned slide its OWN creation, shape and modification ids."""
    counter = itertools.count()

    def uint32(kind: str) -> Callable[[re.Match[str]], str]:
        def swap(match: re.Match[str]) -> str:
            fresh = _fresh_uint32(kind, slide_number, next(counter), match.group(2))
            return f"{match.group(1)}{fresh}{match.group(3)}"

        return swap

    def guid(match: re.Match[str]) -> str:
        fresh = _fresh_guid(slide_number, next(counter), match.group(2))
        return f"{match.group(1)}{fresh}{match.group(3)}"

    xml = P14_CREATION_ID.sub(uint32("p14creation"), xml)
    xml = A16_CREATION_ID.sub(guid, xml)
    xml = P14_MOD_ID.sub(uint32("p14mod"), xml)
    # Each occurrence draws the next counter value, so a table whose columns
    # all carried one id comes out with a distinct id per column.
    xml = A16_COL_ID.sub(uint32("a16col"), xml)
    return A16_ROW_ID.sub(uint32("a16row"), xml)
