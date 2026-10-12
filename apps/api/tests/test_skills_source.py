"""The skills-library adapter: allow-listed, capped, no redirects, indexed search."""

import json
from typing import Any

import httpx
import pytest

from sila.adapters.skills.base import CatalogEntry, SkillSourceError
from sila.adapters.skills.skills_library import SkillsLibraryAdapter

CATALOG = [
    {
        "source": "anthropics/skills",
        "skillId": "pptx",
        "name": "pptx",
        "description": "Create and edit presentation slides",
        "installs": 900,
        "license": "Proprietary",
        "contentType": {"hasScripts": True},
        "dependencies": [{"type": "pip", "name": "python-pptx"}],
    },
    {
        "source": "obra/superpowers",
        "skillId": "brainstorming",
        "name": "brainstorming",
        "description": "Turn ideas into designs through questions",
        "installs": 5000,
    },
    {
        "source": "acme/slides",
        "skillId": "slide-polish",
        "name": "slide-polish",
        "description": "Polish presentation slides",
        "installs": 10,
    },
    {"source": "bad source", "skillId": "x", "name": "x"},  # malformed: skipped
]


class _Web:
    def __init__(self) -> None:
        self.urls: list[str] = []
        self.routes: dict[str, httpx.Response] = {}

    async def send(self, request: httpx.Request) -> httpx.Response:
        self.urls.append(str(request.url))
        response = self.routes.get(str(request.url))
        if response is None:
            return httpx.Response(404, request=request)
        response.request = request
        return response


@pytest.fixture
def web(monkeypatch) -> _Web:
    web = _Web()

    async def _send(_client: httpx.AsyncClient, request: httpx.Request, **_kw: Any):
        return await web.send(request)

    async def _no_dns(url: str, **_kw: Any) -> str:
        return url

    monkeypatch.setattr(httpx.AsyncClient, "send", _send)
    monkeypatch.setattr("sila.adapters.skills.skills_library.validate_external_url", _no_dns)
    monkeypatch.setattr(SkillsLibraryAdapter, "_catalog", None)
    web.routes["https://skills-library.com/api/skills.json"] = httpx.Response(
        200, content=json.dumps(CATALOG).encode()
    )
    return web


def _adapter(**kw: Any) -> SkillsLibraryAdapter:
    return SkillsLibraryAdapter(
        catalog_url="https://skills-library.com/api/skills.json",
        github_api_url="https://api.github.com",
        raw_url="https://raw.githubusercontent.com",
        allowed_hosts=["skills-library.com", "api.github.com", "raw.githubusercontent.com"],
        **kw,
    )


async def test_search_intersects_words_most_installed_first(web: _Web) -> None:
    entries, total = await _adapter().search("presentation slides", page=1, size=10)
    assert [e.skill_key for e in entries] == ["pptx", "slide-polish"]
    assert total == 2
    everything, total_all = await _adapter().search("", page=1, size=10)
    assert [e.skill_key for e in everything][0] == "brainstorming" and total_all == 3
    assert everything[1].has_scripts and everything[1].dependencies[0]["name"] == "python-pptx"


async def test_the_catalog_is_loaded_once(web: _Web) -> None:
    await _adapter().search("a", page=1, size=5)
    await _adapter().entry("obra/superpowers", "brainstorming")
    assert len(web.urls) == 1


async def test_a_host_off_the_list_is_never_contacted(web: _Web) -> None:
    adapter = _adapter()
    adapter._catalog_url = "https://evil.example.com/skills.json"
    with pytest.raises(SkillSourceError, match="not an allowed skills host"):
        await adapter.search("", page=1, size=5)
    assert web.urls == []


async def test_a_redirect_off_the_allow_list_is_never_followed(web: _Web) -> None:
    url = "https://skills-library.com/api/skills.json"
    web.routes[url] = httpx.Response(302, headers={"Location": "http://169.254.169.254/"})
    with pytest.raises(SkillSourceError, match="not an allowed skills host"):
        await _adapter().search("", page=1, size=5)
    assert web.urls == [url], "the metadata address was never requested"


async def test_a_renamed_repo_is_followed_on_an_allowed_host(web: _Web) -> None:
    old = "https://api.github.com/repos/old/name"
    new = "https://api.github.com/repositories/42"
    web.routes[old] = httpx.Response(301, headers={"Location": new})
    web.routes[new] = httpx.Response(200, json={"default_branch": "main"})
    assert await _adapter()._json(old, 10_000) == {"default_branch": "main"}
    assert web.urls == [old, new]


async def test_a_redirect_loop_stops(web: _Web) -> None:
    loop = "https://api.github.com/repos/a/b"
    web.routes[loop] = httpx.Response(301, headers={"Location": loop})
    with pytest.raises(SkillSourceError, match="too many redirects"):
        await _adapter()._json(loop, 10_000)


async def test_a_body_over_the_cap_is_refused(web: _Web) -> None:
    with pytest.raises(SkillSourceError, match="larger than"):
        await _adapter(catalog_max_bytes=100).search("", page=1, size=5)


async def test_the_usual_place_is_read_as_raw_content_without_the_api(web: _Web) -> None:
    """GitHub's API allows 60 calls an hour without a token; raw content is one GET."""
    raw = "https://raw.githubusercontent.com/obra/superpowers/HEAD/skills/brainstorming/SKILL.md"
    web.routes[raw] = httpx.Response(200, content=b"---\nname: brainstorming\n---\nbody")
    entry = await _adapter().entry("obra/superpowers", "brainstorming")
    assert entry is not None
    assert (await _adapter().fetch_skill_md(entry)).startswith("---\nname: brainstorming")
    assert not [u for u in web.urls if "api.github.com" in u]


async def test_a_skill_md_elsewhere_is_found_in_its_repo_tree(web: _Web) -> None:
    """The usual places 404 (unrouted here), so the tree is searched."""
    api = "https://api.github.com/repos/obra/superpowers"
    web.routes[api] = httpx.Response(200, json={"default_branch": "main"})
    web.routes[f"{api}/git/trees/main?recursive=1"] = httpx.Response(
        200,
        json={
            "tree": [
                {"type": "blob", "path": "docs/brainstorming.md"},
                {"type": "blob", "path": "skills/brainstorming/SKILL.md"},
                {"type": "blob", "path": "skills/brainstorming/scripts/run.sh"},
            ]
        },
    )
    raw = "https://raw.githubusercontent.com/obra/superpowers/main/skills/brainstorming/SKILL.md"
    web.routes[raw] = httpx.Response(200, content=b"---\nname: brainstorming\n---\nbody")
    entry = await _adapter().entry("obra/superpowers", "brainstorming")
    assert entry is not None
    text = await _adapter().fetch_skill_md(entry)
    assert text.startswith("---\nname: brainstorming")
    assert not any(u.endswith("run.sh") for u in web.urls), "scripts are never fetched"


async def test_a_path_traversal_reference_is_refused_before_any_request(web: _Web) -> None:
    entry = CatalogEntry(source="obra/..", skill_key="x", name="x", description="", repo_url="")
    with pytest.raises(SkillSourceError, match="not a valid skill reference"):
        await _adapter().fetch_skill_md(entry)
    assert web.urls == []
