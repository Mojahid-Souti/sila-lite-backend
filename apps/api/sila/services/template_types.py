"""The shape of a built-in Bazaar template (see services/bazaar_templates.py).

Its own module so the core catalogue and the Omantel-only catalogue
(bazaar_templates_omantel.py) can both build entries without importing each
other.
"""

from __future__ import annotations

from dataclasses import dataclass

# The OKR Reporting Pack: a pinned session is confidential and runs the pack
# pipeline (services/sila/okr_session.py), not the general agent. Defined here,
# not with the pack, because the chat checks for it even where the pack is absent.
OKR_TEMPLATE_ID = "tpl-okr-pack"


@dataclass(frozen=True)
class BuiltinTemplate:
    id: str
    name: str
    description: str
    tags: tuple[str, ...]
    category_slug: str
    # One line under the chip in the composer, so the user sees what's pinned.
    chip_hint: str
    # What the card's cover advertises (frontend maps these to icons).
    sources: tuple[str, ...]
    outputs: tuple[str, ...]
    # Server-side system-prompt preamble. NEVER exposed to or accepted from clients.
    persona: str
    # Who authored this template, shown as its Bazaar attribution and used to
    # GROUP the templates under that publisher's own heading. Data, not a
    # frontend conditional: a second org's templates need a row here, nothing
    # more. Defaults to the platform itself.
    publisher: str = "Sila"
    # Tool whitelist for the pinned session; () = the mode's full toolset.
    # A gate in code: a staged template must not reach the generic plan/build
    # machinery (live bug: a PDD gap answer got routed into propose_plan).
    tools: tuple[str, ...] = ()
