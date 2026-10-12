"""Which Bazaar category a skill belongs to, from its own name and description.

The upstream catalog has no category field (skills-library derives its own by
pattern), so the category is decided here, once, at import: the category whose
keywords the skill mentions most, the first listed on a tie, "other" when none.
The keys are stable identifiers — the web app shows them through translations.

Complexity: O(words in the text × categories), with the keyword sets as hash sets.
"""

from __future__ import annotations

import re

# (key, keywords) in tie-break order. Keywords are single lowercase words.
CATEGORIES: tuple[tuple[str, frozenset[str]], ...] = (
    (
        "development",
        frozenset(
            [
                "code",
                "coding",
                "developer",
                "api",
                "git",
                "github",
                "debug",
                "debugging",
                "test",
                "testing",
                "typescript",
                "python",
                "javascript",
                "react",
                "frontend",
                "backend",
                "database",
                "sql",
                "deploy",
                "refactor",
                "cli",
                "programming",
                "repository",
                "kubernetes",
                "docker",
            ]
        ),
    ),
    (
        "data",
        frozenset(
            [
                "data",
                "analytics",
                "analysis",
                "analyze",
                "dashboard",
                "chart",
                "charts",
                "metrics",
                "spreadsheet",
                "excel",
                "csv",
                "statistics",
                "statistical",
                "visualization",
                "sql",
                "report",
                "reports",
                "kpi",
            ]
        ),
    ),
    (
        "design",
        frozenset(
            [
                "design",
                "ui",
                "ux",
                "interface",
                "figma",
                "layout",
                "css",
                "tailwind",
                "typography",
                "component",
                "components",
                "brand",
                "branding",
                "accessibility",
                "wireframe",
                "prototype",
            ]
        ),
    ),
    (
        "writing",
        frozenset(
            [
                "writing",
                "write",
                "writer",
                "docs",
                "documentation",
                "document",
                "documents",
                "copy",
                "copywriting",
                "editing",
                "edit",
                "blog",
                "article",
                "essay",
                "proofread",
                "grammar",
                "tone",
                "style",
                "readme",
            ]
        ),
    ),
    (
        "marketing",
        frozenset(
            [
                "marketing",
                "seo",
                "campaign",
                "campaigns",
                "social",
                "content",
                "audience",
                "ads",
                "advertising",
                "brand",
                "growth",
                "newsletter",
                "email",
                "outreach",
                "launch",
            ]
        ),
    ),
    (
        "media",
        frozenset(
            [
                "image",
                "images",
                "video",
                "videos",
                "photo",
                "photos",
                "audio",
                "podcast",
                "animation",
                "illustration",
                "thumbnail",
                "render",
                "rendering",
                "media",
            ]
        ),
    ),
    (
        "productivity",
        frozenset(
            [
                "productivity",
                "planning",
                "plan",
                "plans",
                "meeting",
                "meetings",
                "notes",
                "task",
                "tasks",
                "workflow",
                "schedule",
                "calendar",
                "brainstorming",
                "organize",
                "summary",
                "summarize",
                "presentation",
                "slides",
                "pptx",
                "docx",
                "pdf",
            ]
        ),
    ),
)
OTHER = "other"
CATEGORY_KEYS: tuple[str, ...] = tuple(key for key, _ in CATEGORIES) + (OTHER,)
_WORD = re.compile(r"[a-z]+")


def categorize(name: str, description: str) -> str:
    """The best-matching category key for a skill, or "other"."""
    words = _WORD.findall(f"{name.replace('-', ' ')} {description}".lower())
    best, best_hits = OTHER, 0
    for key, keywords in CATEGORIES:
        hits = sum(1 for word in words if word in keywords)
        if hits > best_hits:
            best, best_hits = key, hits
    return best
