"""Allocating OOXML relationship ids that PowerPoint will accept.

Office always writes `rId` followed by an integer. PowerPoint offers to repair
a file whose relationship ids are anything else, and renders the slides holding
them blank — which is what `rIdOkrChart1` and `rIdSilaChart` did, in two
different builders.

Nothing downstream catches it: such a file is schema-valid, every reference
resolves, and LibreOffice renders a perfect PDF from it. `deck_guard` check 9
is what fails the build now, and this is the allocator that satisfies it.

Complexity: O(number of relationships in the part).
"""

from __future__ import annotations

import re

# Every id, not just well-formed ones. A pattern like `Id="rId(\d+)"` makes a
# malformed id invisible to the allocator, which can then hand out one that
# collides with it — and a verification pattern narrower than the thing it
# verifies is precisely how these ids survived four rounds of review.
_ANY_ID = re.compile(r'Id="([^"]+)"')


def declared_ids(rels_xml: str) -> set[str]:
    """Every relationship id declared in this part, malformed ones included."""
    return set(_ANY_ID.findall(rels_xml))


def allocate_rel_ids(rels_xml: str, count: int) -> list[str]:
    """`count` fresh `rId<n>` ids that collide with nothing in this part.

    Collision is checked against every declared id rather than against the
    numeric maximum, so a part already holding a malformed id still gets safe
    ids out of this (the guard fails the build on the malformed one separately).
    """
    taken = declared_ids(rels_xml)
    highest = max(
        (int(i[3:]) for i in taken if i.startswith("rId") and i[3:].isdigit()),
        default=0,
    )
    out: list[str] = []
    while len(out) < count:
        highest += 1
        candidate = f"rId{highest}"
        if candidate not in taken:
            out.append(candidate)
    return out
