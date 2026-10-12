"""A SKILL.md is untrusted input: parsed safely, refused loudly, never run."""

import pytest

from sila.services.skills.categories import CATEGORY_KEYS, categorize
from sila.services.skills.parse import (
    MAX_INSTRUCTIONS_CHARS,
    SkillParseError,
    parse_skill_md,
)

GOOD = """---
name: brainstorming
description: Turn a rough idea into a design through questions, one at a time.
license: MIT
allowed-tools: Read, Write
---

# Brainstorming

Ask one question at a time.
"""


def test_a_skill_reads_as_its_front_matter_and_instructions() -> None:
    skill = parse_skill_md(GOOD)
    assert skill.name == "brainstorming"
    assert skill.description.startswith("Turn a rough idea")
    assert skill.instructions.startswith("# Brainstorming")
    assert skill.license == "MIT"
    assert skill.allowed_tools == ["Read", "Write"]


def test_no_front_matter_is_refused() -> None:
    with pytest.raises(SkillParseError, match="front matter"):
        parse_skill_md("# Just markdown\n")


def test_yaml_tags_are_never_constructed() -> None:
    """safe_load: a python object tag is an error, not an object."""
    hostile = "---\nname: !!python/object/apply:os.system ['id']\ndescription: x\n---\nbody\n"
    with pytest.raises(SkillParseError, match="not valid YAML"):
        parse_skill_md(hostile)


def test_a_name_and_a_description_are_required() -> None:
    with pytest.raises(SkillParseError, match="'name' and a 'description'"):
        parse_skill_md("---\nname: x\n---\nbody\n")
    with pytest.raises(SkillParseError, match="mapping"):
        parse_skill_md("---\n- a list\n---\nbody\n")


def test_empty_or_oversized_instructions_are_refused() -> None:
    with pytest.raises(SkillParseError, match="no instructions"):
        parse_skill_md("---\nname: x\ndescription: y\n---\n\n")
    too_long = "---\nname: x\ndescription: y\n---\n" + "a" * (MAX_INSTRUCTIONS_CHARS + 1)
    with pytest.raises(SkillParseError, match="limit"):
        parse_skill_md(too_long)


def test_bidi_overrides_and_controls_are_stripped_from_names() -> None:
    skill = parse_skill_md('---\nname: "safe\u202eexe.md"\ndescription: "a\\x07b"\n---\nbody\n')
    assert skill.name == "safeexe.md"
    assert skill.description == "ab"


def test_a_windows_file_with_a_bom_reads() -> None:
    assert parse_skill_md("\ufeff" + GOOD.replace("\n", "\r\n")).name == "brainstorming"


@pytest.mark.parametrize(
    ("name", "description", "category"),
    [
        ("pptx", "Create and edit presentation slides", "productivity"),
        ("frontend-design", "Design polished UI components with Tailwind", "design"),
        ("seo-audit", "Audit a site's SEO and marketing campaign copy", "marketing"),
        ("debugging", "Systematic debugging of Python code", "development"),
        ("csv-insights", "Analyze a CSV and chart the metrics", "data"),
        ("mystery", "Something else entirely", "other"),
    ],
)
def test_categories_come_from_the_skills_own_words(name, description, category) -> None:
    assert categorize(name, description) == category
    assert category in CATEGORY_KEYS
