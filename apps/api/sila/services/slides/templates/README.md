# Corporate .pptx templates

`omantel.pptx` — the Omantel corporate deck template (12 designed slides).

## What this file actually is (measured, 2026-09-07)

Worth reading before wiring it into the engine, because it is **not** shaped
the way a PowerPoint template usually is:

| | Finding |
|---|---|
| Slide masters | 1, stock |
| Slide layouts | 11, **all stock Office defaults** ("Title Slide", "Title and Content", …) |
| Theme | **stock Office 2024** — accent1 `#156082`, fonts Aptos / Aptos Display |
| Designed slides | 12 — and **11 of the 12 sit on the "Blank" layout** |
| Real brand colours | `#2D13EA` blue, `#FF7800` orange, `#1D1D1D` ink — only ever as per-shape fills |
| Real brand fonts | FS Alvar, Roboto Slab, Roboto — only ever as per-run overrides |
| Media | 7 images (logo, marks) referenced per slide, not by the master |

**The branding lives in hand-drawn shapes on each slide, not in the layouts or
the theme.** The designer built each slide by placing shapes on a blank canvas.

### Consequence 1 — `brand_extract.py` reads the wrong thing for this file

`extract_brand_tokens()` reads the THEME, which is correct for a normally-built
corporate template and wrong for this one. Run against `omantel.pptx` it returns:

```
accent='156082'  display_font='Aptos Display'  body_font='Aptos'
```

Microsoft's stock teal and Microsoft's stock font — not Omantel's. It would
produce a stock-Office deck and label it branded. Not a bug in the extractor;
the file simply carries no brand in the place it looks.

### Consequence 2 — the way to use this template is CLONE-AND-FILL

Generating a new slide "against a layout" inherits nothing here, because the
layouts are blank. What works is taking a designed slide as a stencil, keeping
every shape, image, colour and coordinate, and rewriting only its text.

Verified end to end: cloning slide 2, swapping the heading, registering the part
in `[Content_Types].xml` / `presentation.xml` / `presentation.xml.rels`, then
rendering through LibreOffice reproduces the logo, the blue arrow, the orange
diamonds and the rule pixel-identically, with only the heading changed.

### Trap for whoever implements it

Text is split across runs. The header on slide 2 is **three** `<a:r>` elements —
`'Presentation'`, `' '`, `'name'` — so a naive `xml.replace("Presentation name", …)`
matches nothing and fails silently. Fill must walk each `<a:p>`, write the new
text into the first run and drop the rest, so the run's formatting survives.

## How it is wired (implemented 2026-09-07)

`create_slide_deck` asks `template_fill.stencil_book_for(bytes)` whether a
template is *designed*. If it is, the deck is rendered by
`template_fill.fill_template` from stencils instead of by `render.render_deck`
with an extracted palette — the theme path is skipped entirely, because for this
file it yields stock Office teal.

Detection is by a marker typeface (`FS Alvar`) plus a structural check that every
stencil's slide and shape index still exists, so a re-authored template falls
back to the palette path rather than writing text into whatever shape now sits
at that position.

`template_map.deck_to_slides` translates a generated `schema.Deck` into stencil
slides. Its rules are the ones that stop a deck looking machine-made: a card is
always a label AND a sentence, a long sentence is shortened at a word boundary
rather than spilled into the next box, and the card count comes from the stencil
(so there is never a `(cont.)` slide or a spare card holding a caveat).

## Stencil inventory

| Kind | Slide | Carries |
|---|---|---|
| `title` | 1 | Title slide, light-trails artwork |
| `section` | 2 | Section heading |
| `statement` | 11 | Full-bleed statement |
| `points` | 8 | Title + up to 3 diamond-node cards |
| `points4` | 7 | 4 across; borrows its title shape from slide 8 |
| `table` | 9 | Title + a table with the brand-blue header |

Not yet stencilled: 3, 4, 5, 6, 10, 12. Slide 6 (SWOT) is deliberately left out —
its axis labels ("Positive"/"Internal"/…) are part of the drawing, so it only
suits genuinely SWOT-shaped content.

### Fitting

A stencil's boxes are sized for its own placeholder, not for real copy, so each
declares `title_size` / `title_at` (the box it actually needs) and `title_chars`
(the length past which type scales down). These were tuned by rendering through
LibreOffice and looking at every slide, which is the only way collisions with the
drawn decoration show up — a headline overrunning into a diamond typechecks
perfectly.
