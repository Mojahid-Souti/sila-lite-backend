"""Import -> scan -> review -> publish: nothing unvetted reaches the Bazaar."""

import uuid
from typing import Any
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException

from sila.adapters.scanner.base import Finding
from sila.adapters.skills.base import CatalogEntry, SkillSourceError
from sila.db.models import Skill
from sila.services.skills.service import SkillService

MD = "---\nname: brainstorming\ndescription: Turn ideas into designs.\n---\n# Ask one question.\n"
ENTRY = CatalogEntry(
    source="obra/superpowers",
    skill_key="brainstorming",
    name="brainstorming",
    description="",
    repo_url="https://github.com/obra/superpowers",
    installs=5000,
)


class _Source:
    def __init__(self, text: str = MD, *, fail: bool = False) -> None:
        self.text, self.fail = text, fail

    async def search(self, query: str, *, page: int, size: int):
        return [ENTRY], 1

    async def entry(self, source: str, skill_key: str):
        return ENTRY if (source, skill_key) == (ENTRY.source, ENTRY.skill_key) else None

    async def fetch_skill_md(self, entry: CatalogEntry) -> str:
        if self.fail:
            raise SkillSourceError("github down")
        return self.text


class _Scanner:
    def __init__(self, name: str, severity: str | None = None, *, crash: bool = False) -> None:
        self.name, self.severity, self.crash = name, severity, crash

    async def scan(self, _input: Any) -> list[Finding]:
        if self.crash:
            raise RuntimeError("ollama unreachable")
        if self.severity is None:
            return []
        return [Finding("injection.role_override", self.severity, "skill", "x", "ignore previous")]


class _Repo:
    rows: dict[uuid.UUID, Skill] = {}
    recent_imports = 0

    def __init__(self, _db: object) -> None: ...

    async def get(self, skill_id: uuid.UUID) -> Skill | None:
        return _Repo.rows.get(skill_id)

    async def get_by_upstream(self, source: str, skill_key: str) -> Skill | None:
        return next(
            (s for s in _Repo.rows.values() if (s.source, s.skill_key) == (source, skill_key)),
            None,
        )

    async def imports_since(self, _user: uuid.UUID, _since: object) -> int:
        return _Repo.recent_imports

    def add(self, skill: Skill) -> None:
        skill.id = uuid.uuid4()
        _Repo.rows[skill.id] = skill

    async def delete(self, skill: Skill) -> None:
        del _Repo.rows[skill.id]


class _Audit:
    actions: list[str] = []

    def __init__(self, _db: object) -> None: ...

    async def record(self, *, action: str, **_kw: Any) -> None:
        _Audit.actions.append(str(action))


@pytest.fixture(autouse=True)
def _fakes(monkeypatch) -> list[dict[str, Any]]:
    notes: list[dict[str, Any]] = []

    async def _notify(_db: object, **kw: Any) -> None:
        notes.append(kw)

    monkeypatch.setattr("sila.services.skills.service.SkillRepository", _Repo)
    monkeypatch.setattr("sila.services.skills.service.AuditRepository", _Audit)
    monkeypatch.setattr("sila.services.skills.service.notify", _notify)
    _Repo.rows, _Repo.recent_imports, _Audit.actions = {}, 0, []
    return notes


WS, ADMIN, REVIEWER = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()


def _svc(source: _Source | None = None, *scanners: _Scanner) -> SkillService:
    return SkillService(AsyncMock(), source or _Source(), list(scanners) or [_Scanner("clean")])


async def _import(svc: SkillService, workspace: uuid.UUID = WS) -> Skill:
    return await svc.import_skill(
        workspace_id=workspace, user_id=ADMIN, source=ENTRY.source, skill_key=ENTRY.skill_key
    )


async def test_a_clean_import_waits_for_a_reviewer_and_is_not_published(_fakes) -> None:
    skill = await _import(_svc())
    assert skill.vetting_status == "needs_review" and not skill.published
    assert skill.body == "# Ask one question." and skill.category == "productivity"
    assert _Audit.actions == ["skill.import"]
    assert _fakes[0]["title"] == "Skill imported: brainstorming"


async def test_a_high_finding_rejects_the_import() -> None:
    skill = await _import(_svc(None, _Scanner("regex", "high")))
    assert skill.vetting_status == "rejected"
    assert skill.findings[0]["type"] == "injection.role_override"


async def test_a_scanner_that_fails_is_a_finding_not_a_pass() -> None:
    skill = await _import(_svc(None, _Scanner("regex"), _Scanner("llm", crash=True)))
    assert skill.vetting_status == "rejected"
    assert skill.findings[0]["type"] == "scan.incomplete"
    # Importing again rescans even though the content is the same.
    again = await _import(_svc(None, _Scanner("regex"), _Scanner("llm")))
    assert again.vetting_status == "needs_review" and again.id == skill.id


async def test_the_same_content_is_a_no_op_and_new_content_re_vets() -> None:
    svc = _svc()
    skill = await _import(svc)
    await svc.approve(WS, skill.id, REVIEWER)
    assert skill.published
    assert (await _import(svc)).published, "unchanged: stays published"
    changed = await _import(_svc(_Source(MD + "\nNew step.\n")))
    assert changed.vetting_status == "needs_review" and not changed.published


async def test_another_workspace_cannot_take_over_an_imported_skill() -> None:
    await _import(_svc())
    with pytest.raises(HTTPException) as caught:
        await _import(_svc(), uuid.uuid4())
    assert caught.value.status_code == 409


async def test_imports_are_rate_limited() -> None:
    _Repo.recent_imports = 10
    with pytest.raises(HTTPException) as caught:
        await _import(_svc())
    assert caught.value.status_code == 429


async def test_a_skill_the_catalog_does_not_list_is_imported_by_reference() -> None:
    svc = _svc()
    skill = await svc.import_skill(
        workspace_id=WS, user_id=ADMIN, source="coreyhaines31/marketingskills", skill_key="copy"
    )
    assert skill.name == "brainstorming", "named by its own SKILL.md"
    assert skill.repo_url == "https://github.com/coreyhaines31/marketingskills"
    assert skill.installs == 0 and skill.vetting_status == "needs_review"


async def test_upstream_trouble_and_bad_files_are_named() -> None:
    with pytest.raises(HTTPException) as down:
        await _import(_svc(_Source(fail=True)))
    assert down.value.status_code == 502
    with pytest.raises(HTTPException) as bad:
        await _import(_svc(_Source("# no front matter")))
    assert bad.value.status_code == 422 and "front matter" in bad.value.detail


async def test_only_an_approved_skill_is_published() -> None:
    svc = _svc()
    skill = await _import(svc)
    with pytest.raises(HTTPException) as caught:
        await svc.set_published(WS, skill.id, REVIEWER, published=True)
    assert caught.value.status_code == 409
    await svc.approve(WS, skill.id, REVIEWER)
    await svc.set_published(WS, skill.id, REVIEWER, published=False)
    assert not skill.published
    await svc.reject(WS, skill.id, REVIEWER)
    with pytest.raises(HTTPException):
        await svc.set_published(WS, skill.id, REVIEWER, published=True)
    assert _Audit.actions[-3:] == ["skill.approve", "skill.unpublish", "skill.reject"]


async def test_a_rejected_skill_cannot_be_approved() -> None:
    svc = _svc(None, _Scanner("regex", "critical"))
    skill = await _import(svc)
    with pytest.raises(HTTPException) as caught:
        await svc.approve(WS, skill.id, REVIEWER)
    assert caught.value.status_code == 409


async def test_a_rescan_re_vets_and_unpublishes() -> None:
    svc = _svc()
    skill = await _import(svc)
    await svc.approve(WS, skill.id, REVIEWER)
    flagged = SkillService(AsyncMock(), _Source(), [_Scanner("regex", "high")])
    again = await flagged.rescan(WS, skill.id, REVIEWER)
    assert again.vetting_status == "rejected" and not again.published
    assert _Audit.actions[-1] == "skill.rescan"


async def test_no_verdict_from_the_skill_review_rejects_the_import() -> None:
    """The model answering the document instead of reviewing it is not a pass."""
    from sila.adapters.scanner.skill_review import SkillLLMScanner

    class _Answers:
        async def complete_json(self, **_kw: Any) -> dict[str, Any]:
            return {"error": "please ask a specific question"}  # no findings list

    reviewer = SkillLLMScanner(_Answers())  # type: ignore[arg-type]
    skill = await _import(SkillService(AsyncMock(), _Source(), [reviewer]))
    assert skill.vetting_status == "rejected"
    assert skill.findings[0]["type"] == "scan.incomplete"


def test_skills_are_read_by_the_skill_reviewer_not_the_agent_one() -> None:
    from sila.adapters.scanner.llm import LLMScanner
    from sila.adapters.scanner.skill_review import SkillLLMScanner
    from sila.services.skills.service import skill_scanners

    kinds = [type(s) for s in skill_scanners()]
    assert SkillLLMScanner in kinds and LLMScanner not in kinds


async def test_a_removed_skill_is_gone_and_audited() -> None:
    svc = _svc()
    skill = await _import(svc)
    await svc.approve(WS, skill.id, REVIEWER)
    await svc.remove(WS, skill.id, REVIEWER)
    assert skill.id not in _Repo.rows
    assert _Audit.actions[-1] == "skill.remove"
    with pytest.raises(HTTPException) as caught:
        await svc.remove(WS, skill.id, REVIEWER)
    assert caught.value.status_code == 404


async def test_another_workspace_cannot_remove_a_skill() -> None:
    skill = await _import(_svc())
    with pytest.raises(HTTPException) as caught:
        await _svc().remove(uuid.uuid4(), skill.id, REVIEWER)
    assert caught.value.status_code == 404
    assert skill.id in _Repo.rows


async def test_decisions_stay_inside_the_owning_workspace() -> None:
    skill = await _import(_svc())
    with pytest.raises(HTTPException) as caught:
        await _svc().approve(uuid.uuid4(), skill.id, REVIEWER)
    assert caught.value.status_code == 404
