"""A SKILL.md, read safely: its front matter and its instructions.

An Agent Skill is a markdown file whose front matter (between two `---` lines)
names it and says when to use it; the rest is instructions for the model. The
file comes from a third-party repository, so it is untrusted input:

  * the front matter is parsed with `yaml.safe_load` only, and must be a
    mapping with a non-empty `name` and `description`;
  * nothing in it is executed, fetched or followed — links stay text;
  * the instructions are capped, and a file over the cap is refused rather
    than silently cut (a cut instruction can change its meaning).

Complexity: O(file size).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

import yaml

# Longest instructions accepted, in characters. What a chat actually carries is
# capped separately (settings.sila_skill_prompt_chars).
MAX_INSTRUCTIONS_CHARS = 40_000
_NAME_CHARS = 120
_DESCRIPTION_CHARS = 1_000
_FRONT_MATTER = re.compile(r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.DOTALL)
# C0/C1 controls except tab and newline, and the bidi overrides (RTL-override
# attacks in names shown in the Bazaar).
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


class SkillParseError(ValueError):
    """The file is not a SKILL.md Sila can use."""


@dataclass(frozen=True)
class ParsedSkill:
    name: str
    description: str
    instructions: str
    license: str | None = None
    allowed_tools: list[str] = field(default_factory=list)


def parse_skill_md(text: str) -> ParsedSkill:
    """The front matter and instructions of a SKILL.md, or SkillParseError."""
    match = _FRONT_MATTER.match(text)
    if match is None:
        raise SkillParseError("no front matter: a SKILL.md starts with a '---' block")
    try:
        meta = yaml.safe_load(match.group(1))
    except yaml.YAMLError as exc:
        raise SkillParseError(f"the front matter is not valid YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise SkillParseError("the front matter must be a mapping of fields")

    name = clean_line(str(meta.get("name") or ""), _NAME_CHARS)
    description = clean_line(str(meta.get("description") or ""), _DESCRIPTION_CHARS)
    if not name or not description:
        raise SkillParseError("the front matter needs a 'name' and a 'description'")

    instructions = _CONTROL.sub("", text[match.end() :]).strip()
    if not instructions:
        raise SkillParseError("the skill has no instructions after its front matter")
    if len(instructions) > MAX_INSTRUCTIONS_CHARS:
        raise SkillParseError(
            f"the instructions are {len(instructions):,} characters; the limit is "
            f"{MAX_INSTRUCTIONS_CHARS:,}"
        )
    return ParsedSkill(
        name=name,
        description=description,
        instructions=instructions,
        license=_license(meta.get("license")),
        allowed_tools=_tools(meta.get("allowed-tools") or meta.get("allowed_tools")),
    )


def clean_line(value: str, limit: int) -> str:
    """One line of display text: no control or bidi-override characters, capped."""
    return " ".join(_CONTROL.sub("", value).split())[:limit]


def _license(raw: object) -> str | None:
    """The declared licence, or None when the skill states none."""
    text = clean_line(str(raw), 120) if raw else ""
    return text or None


def _tools(raw: object) -> list[str]:
    if isinstance(raw, str):
        raw = raw.replace(",", " ").split()
    if not isinstance(raw, list):
        return []
    return [clean_line(str(tool), 60) for tool in raw[:20] if str(tool).strip()]
