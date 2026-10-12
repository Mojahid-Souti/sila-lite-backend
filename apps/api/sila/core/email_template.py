"""Formal HTML email rendering for SILA's outbound send_email tool.

`render_sila_email` wraps the model's message in a restrained, enterprise-grade
HTML shell: the real Sila lockup (embedded as an inline CID image so it renders
in Outlook/Gmail, which strip data-URIs and can't reach localhost), a monochrome
palette, Markdown-lite body (h1/h2/h3, bold, bullets, links) and a real button
for the primary call-to-action. Table layout + inline styles are deliberate —
the only markup that survives mail-client sanitizers. Brand values come from
settings so ops can rebrand via env (Rule 5). O(n) over the body length.
"""

from __future__ import annotations

import html
import pathlib
import re

from sila.core.config import settings

# The inline logo travels WITH the email (Content-ID), so it renders where
# external/data-URI images don't. sila_logo_inline() hands the bytes to the send.
SILA_LOGO_CID = "sila-logo"
_LOGO_PATH = pathlib.Path(__file__).parent / "assets" / "sila-logo.png"

_URL_RE = re.compile(r"(https?://[^\s<>\"')]+)")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_BOLD_RE = re.compile(r"\*\*(.+?)\*\*")

# Monochrome, enterprise — deliberately NO brand purple in the mail body.
_INK = "#111827"  # near-black: headings + button
_TEXT = "#374151"  # body copy
_MUTED = "#6b7280"  # footer / meta
_BORDER = "#e5e7eb"
_BG = "#f4f4f5"
_LINK = "#1d4ed8"  # restrained blue for inline links (never purple)
_FONT = "-apple-system,'Segoe UI',Roboto,Helvetica,Arial,sans-serif"


def sila_logo_inline() -> tuple[str, bytes, str] | None:
    """(content_id, png_bytes, mime) for the inline logo, or None if unreadable
    (the shell then falls back to a text wordmark). Read once per send; tiny."""
    if settings.sila_email_logo_url:
        return None  # a hosted <img> is used instead — no attachment needed
    try:
        return (SILA_LOGO_CID, _LOGO_PATH.read_bytes(), "image/png")
    except OSError:
        return None


def _inline(text: str) -> str:
    """Escape a plain-text run, then apply inline Markdown: **bold**, [text](url),
    and bare URLs. Escaping first means model/user text can never inject markup —
    only our own tags are added afterwards."""
    escaped = html.escape(text)
    anchors: list[str] = []

    def _stash_md(m: re.Match[str]) -> str:
        label, href = m.group(1), m.group(2).replace("&amp;", "&")
        anchors.append(
            f'<a href="{href}" style="color:{_LINK};text-decoration:underline;'
            f'word-break:break-all;">{label}</a>'
        )
        return f"\x00{len(anchors) - 1}\x00"

    escaped = _MD_LINK_RE.sub(_stash_md, escaped)
    escaped = _URL_RE.sub(
        lambda m: (
            f'<a href="{m.group(1).replace("&amp;", "&")}" '
            f'style="color:{_LINK};text-decoration:underline;word-break:break-all;">{m.group(1)}</a>'
        ),
        escaped,
    )
    escaped = _BOLD_RE.sub(rf'<strong style="color:{_INK};">\1</strong>', escaped)
    return re.sub(r"\x00(\d+)\x00", lambda m: anchors[int(m.group(1))], escaped)


def _h(level: int, text: str) -> str:
    style = {
        1: f"margin:0 0 12px;font-size:22px;font-weight:700;color:{_INK};letter-spacing:-0.02em;",
        2: f"margin:22px 0 8px;font-size:17px;font-weight:600;color:{_INK};letter-spacing:-0.01em;",
        3: f"margin:20px 0 6px;font-size:12px;font-weight:700;color:{_MUTED};"
        "text-transform:uppercase;letter-spacing:0.06em;",
    }[level]
    return f'<h{level} style="{style}">{_inline(text)}</h{level}>'


def _body_html(body: str) -> str:
    """Markdown-lite → HTML blocks. `#`/`##`/`###` are h1/h2/h3, `- `/`* ` lines
    group into a bullet list, blank lines split paragraphs, single newlines are
    line breaks within a paragraph. O(lines)."""
    out: list[str] = []
    para: list[str] = []
    bullets: list[str] = []

    def flush_para() -> None:
        if para:
            joined = "<br>".join(_inline(line) for line in para)
            out.append(
                f'<p style="margin:0 0 14px;font-size:15px;line-height:1.65;'
                f'color:{_TEXT};">{joined}</p>'
            )
            para.clear()

    def flush_bullets() -> None:
        if bullets:
            items = "".join(
                f'<li style="margin:0 0 6px;font-size:15px;line-height:1.6;'
                f'color:{_TEXT};">{_inline(b)}</li>'
                for b in bullets
            )
            out.append(f'<ul style="margin:0 0 14px;padding-left:20px;">{items}</ul>')
            bullets.clear()

    for raw in body.strip().split("\n"):
        stripped = raw.strip()
        if not stripped:
            flush_para()
            flush_bullets()
        elif stripped.startswith("### "):
            flush_para(), flush_bullets(), out.append(_h(3, stripped[4:]))
        elif stripped.startswith("## "):
            flush_para(), flush_bullets(), out.append(_h(2, stripped[3:]))
        elif stripped.startswith("# "):
            flush_para(), flush_bullets(), out.append(_h(1, stripped[2:]))
        elif stripped.startswith(("- ", "* ")):
            flush_para()
            bullets.append(stripped[2:])
        else:
            flush_bullets()
            para.append(stripped)
    flush_para()
    flush_bullets()
    return "\n".join(out)


def _cta_button(label: str, url: str) -> str:
    """A bulletproof table button (bgcolor on the <td> renders in Outlook too)."""
    return (
        '<table role="presentation" cellspacing="0" cellpadding="0" '
        'style="margin:4px 0 20px;"><tr>'
        f'<td align="center" bgcolor="{_INK}" style="border-radius:8px;">'
        f'<a href="{html.escape(url)}" style="display:inline-block;padding:13px 28px;'
        "font-size:14px;font-weight:600;line-height:1;color:#ffffff;text-decoration:none;"
        f'font-family:{_FONT};">{html.escape(label)}</a>'
        "</td></tr></table>"
    )


def _logo_cell() -> str:
    """The Sila lockup: a hosted <img> when configured, else the inline CID image,
    with a text wordmark as the alt so it degrades gracefully."""
    brand = html.escape(settings.sila_email_brand)
    src = settings.sila_email_logo_url or f"cid:{SILA_LOGO_CID}"
    return (
        f'<img src="{html.escape(src)}" alt="{brand}" height="30" '
        'style="display:block;border:0;height:30px;">'
    )


def render_sila_email(
    *, body: str, greeting: str | None = None, cta: dict[str, str] | None = None
) -> str:
    """Wrap `body` (Markdown-lite) in the enterprise Sila shell. `greeting` is an
    optional opening line; `cta` is an optional {label, url} rendered as a button
    after the body. The footer carries brand/team/phone/contact from settings."""
    brand = html.escape(settings.sila_email_brand)
    tagline = html.escape(settings.sila_email_tagline)
    team = html.escape(settings.sila_email_team)
    phone = html.escape(settings.sila_email_phone)
    contact = html.escape(settings.sila_email_contact)

    greeting_html = (
        f'<p style="margin:0 0 16px;font-size:15px;line-height:1.65;color:{_TEXT};">'
        f"{_inline(greeting)}</p>"
        if greeting and greeting.strip()
        else ""
    )
    cta_html = (
        _cta_button(cta["label"], cta["url"]) if cta and cta.get("label") and cta.get("url") else ""
    )

    return f"""\
<!doctype html>
<html>
<body style="margin:0;padding:0;background:{_BG};">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" \
style="background:{_BG};padding:40px 0;">
  <tr><td align="center">
    <table role="presentation" width="600" cellpadding="0" cellspacing="0" \
style="max-width:600px;width:100%;background:#ffffff;border:1px solid {_BORDER};\
border-radius:10px;overflow:hidden;font-family:{_FONT};">
      <tr><td style="padding:32px 40px 20px;border-bottom:1px solid {_BORDER};">\
{_logo_cell()}</td></tr>
      <tr><td style="padding:28px 40px 8px;">
        {greeting_html}
        {_body_html(body)}
        {cta_html}
      </td></tr>
      <tr><td style="padding:4px 40px 0;"><hr style="border:0;border-top:1px solid \
{_BORDER};margin:0;"></td></tr>
      <tr><td style="padding:22px 40px 28px;">
        <p style="margin:0 0 3px;font-size:14px;font-weight:600;color:{_INK};">{team}</p>
        <p style="margin:0 0 3px;font-size:13px;color:{_MUTED};">{brand} — {tagline}</p>
        <p style="margin:0;font-size:13px;color:{_MUTED};">
          {phone} &nbsp;·&nbsp;
          <a href="mailto:{contact}" style="color:{_MUTED};text-decoration:underline;">{contact}</a>
        </p>
      </td></tr>
      <tr><td style="padding:14px 40px;background:#fafafa;border-top:1px solid {_BORDER};">
        <p style="margin:0;font-size:11px;color:{_MUTED};line-height:1.5;">
          Sent by {brand} on behalf of the {contact} mailbox.
        </p>
      </td></tr>
    </table>
  </td></tr>
</table>
</body>
</html>"""
