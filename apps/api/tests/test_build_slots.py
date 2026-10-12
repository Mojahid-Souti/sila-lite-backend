"""Part B — the deterministic requirements-interview gate.

The build tool must REFUSE to build until the agent type is classified, every
must_ask slot is filled, the destination for a wired connector is captured, and the
user confirmed the reflected spec — regardless of what the model tries (a weak model
can't skip the interview by calling build early). No DB/n8n needed — the gate
short-circuits before any of that.
"""

from __future__ import annotations

import uuid

from sila.adapters.auth.base import UserResult
from sila.api.v1.sila import _record_slot_answer
from sila.core.constants import Role
from sila.services.sila import build_slots as bs
from sila.services.sila.tool_registry import ToolContext, _requirements_gate


def _ctx(*, interactive: bool = True, build_state: dict | None = None) -> ToolContext:
    return ToolContext(
        db=None,  # type: ignore[arg-type]
        user=UserResult(id=uuid.uuid4(), email="t@t.dev", email_confirmed=True),
        workspace_id=uuid.uuid4(),
        role=Role.BUILDER,
        interactive=interactive,
        build_state=build_state if build_state is not None else {},
    )


# --- classify --------------------------------------------------------------


def test_classify_agent_type() -> None:
    assert bs.classify_agent_type("post a daily summary to slack", []) == bs.SCHEDULED_PUBLISHER
    assert bs.classify_agent_type("answer HR questions from the handbook", []) == bs.KNOWLEDGE
    assert bs.classify_agent_type("when a ticket arrives, notify me", []) == bs.MONITOR
    assert bs.classify_agent_type("run a multi-step pipeline", []) == bs.WORKFLOW


def test_classify_uses_integrations_when_purpose_is_thin() -> None:
    # A thin purpose the model dropped the posting intent from, but Slack is wired:
    # the destination integration alone marks it a publisher (not knowledge).
    assert bs.classify_agent_type("answer from my docs", ["slack"]) == bs.SCHEDULED_PUBLISHER
    # Event-driven phrasing still wins (a monitor that posts to Slack).
    assert bs.classify_agent_type("when a ticket arrives, alert", ["slack"]) == bs.MONITOR
    # A wired email destination is also a publisher signal (same shape, different dest).
    assert bs.classify_agent_type("send me a summary", ["email"]) == bs.SCHEDULED_PUBLISHER


# --- next_missing_slot -----------------------------------------------------


def test_next_missing_walks_must_ask_and_skips_defaultable() -> None:
    filled: dict = {}
    s1 = bs.next_missing_slot(bs.KNOWLEDGE, filled, [])
    assert s1 and s1.name == "out_of_scope"
    filled["out_of_scope"] = "x"
    s2 = bs.next_missing_slot(bs.KNOWLEDGE, filled, [])
    assert s2 and s2.name == "escalation"
    filled["escalation"] = "y"
    # "name" is defaultable → not asked → complete.
    assert bs.next_missing_slot(bs.KNOWLEDGE, filled, []) is None


def test_destination_slot_only_required_when_provider_wired() -> None:
    filled = {"content": "sections", "schedule": "weekdays 9am"}
    # No slack integration → destination not required → complete (rest defaultable).
    assert bs.next_missing_slot(bs.SCHEDULED_PUBLISHER, filled, []) is None
    # Slack wired → the channel destination becomes required.
    slot = bs.next_missing_slot(bs.SCHEDULED_PUBLISHER, filled, ["slack"])
    assert slot and slot.name == "destination" and slot.destination_for == "slack"
    # …but when Slack is an OAuth connector, the channel comes from the Connect picker,
    # not a typed slot — skip it so the user isn't asked the channel twice.
    assert (
        bs.next_missing_slot(
            bs.SCHEDULED_PUBLISHER, filled, ["slack"], skip_destination_for={"slack"}
        )
        is None
    )


def test_email_recipient_required_when_email_wired() -> None:
    # email wired → the recipient (email_to) slot becomes required, like the Slack channel.
    filled = {"content": "sections", "schedule": "daily 9am"}
    slot = bs.next_missing_slot(bs.SCHEDULED_PUBLISHER, filled, ["email"])
    assert slot and slot.name == "email_to" and slot.destination_for == "email"
    # Not wired → not asked (rest defaultable → complete).
    assert bs.next_missing_slot(bs.SCHEDULED_PUBLISHER, filled, []) is None


def test_google_targets_required_when_wired() -> None:
    # Gmail/Sheets have NO Connect resource picker (least-privilege scopes), so the
    # interview is the ONLY place their target is captured — the slot must be required.
    filled = {"content": "sections", "schedule": "daily 9am"}
    slot = bs.next_missing_slot(bs.SCHEDULED_PUBLISHER, filled, ["gmail"])
    assert slot and slot.name == "gmail_to" and slot.destination_for == "gmail"
    slot = bs.next_missing_slot(bs.SCHEDULED_PUBLISHER, filled, ["google-sheets"])
    assert slot and slot.name == "sheet_id" and slot.destination_for == "google-sheets"
    assert bs.next_missing_slot(bs.SCHEDULED_PUBLISHER, filled, []) is None


def test_classify_google_destinations_are_publisher_signals() -> None:
    # A wired Gmail/Sheets/Chat/Tasks destination marks a publisher even from a
    # thin purpose.
    assert bs.classify_agent_type("answer from my docs", ["gmail"]) == bs.SCHEDULED_PUBLISHER
    assert bs.classify_agent_type("log results", ["google-sheets"]) == bs.SCHEDULED_PUBLISHER
    assert bs.classify_agent_type("share updates", ["google-chat"]) == bs.SCHEDULED_PUBLISHER
    assert bs.classify_agent_type("track my todos", ["google-tasks"]) == bs.SCHEDULED_PUBLISHER


def test_chat_space_required_when_google_chat_wired() -> None:
    filled = {"content": "sections", "schedule": "daily 9am"}
    slot = bs.next_missing_slot(bs.SCHEDULED_PUBLISHER, filled, ["google-chat"])
    assert slot and slot.name == "chat_space" and slot.destination_for == "google-chat"
    # google-tasks needs no target (default list) → nothing more to ask.
    assert bs.next_missing_slot(bs.SCHEDULED_PUBLISHER, filled, ["google-tasks"]) is None


def test_normalize_destination_extracts_sheet_id() -> None:
    url = "https://docs.google.com/spreadsheets/d/1AbC_d-EfG9/edit#gid=0"
    assert bs.normalize_destination("sheet_id", url) == "1AbC_d-EfG9"
    assert bs.normalize_destination("sheet_id", "1AbC_d-EfG9") == "1AbC_d-EfG9"
    # Other slots pass through (trimmed).
    assert bs.normalize_destination("gmail_to", " boss@example.com ") == "boss@example.com"


def test_detect_goal_language() -> None:
    assert bs.detect_goal_language("build an agent that emails a digest") == "en"
    assert bs.detect_goal_language("ابنِ لي وكيلاً يرسل ملخصاً يومياً") == "ar"
    assert bs.detect_goal_language("") == "en"


def test_summary_instruction_states_language() -> None:
    # The output language is ALWAYS explicit — unstated, a small model picks one at
    # random (an English ask once produced an Arabic digest).
    from sila.services.sila.workflow_assembly import summary_instruction_for

    en = summary_instruction_for("A different section each day", "en")
    assert en.startswith("A different section each day") and en.endswith("Write in English.")
    ar = summary_instruction_for("", "ar")
    assert "3-5" in ar and ar.endswith("Write in Arabic.")


async def test_goal_named_connector_overrides_model_integration_choice() -> None:
    # "via Gmail" in the user's own words must wire the gmail CONNECTOR even when the
    # model passed the generic platform email (the MailHog-instead-of-Gmail bug).
    from sila.services.sila.tool_registry import _create_agent_workflow

    ctx = _ctx(
        build_state={"goal": "Build an agent that emails me a daily digest of my docs via Gmail"}
    )
    res = await _create_agent_workflow(
        ctx, {"name": "Digest", "purpose": "daily digest", "integrations": ["email"]}
    )
    assert ctx.build_state["build_args"]["integrations"] == ["gmail"]
    assert res.ui is not None  # paused at the interview — nothing was built


def test_extract_correction_pulls_the_value_out_of_the_sentence() -> None:
    # "the name should be: My first Agent" once became the literal agent name.
    assert bs.extract_correction("name", "the name should be: My first Agent") == "My first Agent"
    assert bs.extract_correction("name", "call it Digest Bot") == "Digest Bot"
    assert bs.extract_correction("name", "My first Agent") == "My first Agent"
    assert bs.extract_correction("gmail_to", "send it to boss@example.com please") == (
        "boss@example.com"
    )
    assert bs.extract_correction("destination", "post to #general instead") == "#general"
    # Schedule phrases pass through (parsed downstream by _schedule_to_cron).
    assert bs.extract_correction("schedule", "weekdays at 5pm") == "weekdays at 5pm"


async def test_change_name_applies_to_spec_and_build_args() -> None:
    # The changed name must show in the reflected spec AND win in the build (the
    # agent was once registered under the old name despite the confirmed change).
    state: dict = {
        "agent_type": bs.SCHEDULED_PUBLISHER,
        "docs_asked": True,
        "filled": {"content": "sections", "schedule": "0 9 * * 1-5"},
        "change_slot": "the name should be: My first Agent",
        "confirmed": True,
    }
    ctx = _ctx(build_state=state)
    g = await _requirements_gate(ctx, "Daily Digest Agent", "daily digest", [], None, _M)
    assert state["filled"]["name"] == "My first Agent"
    assert g is not None and g.ui["questions"][0]["id"] == "__confirm__"


def test_record_slot_answer_invalidates_stale_confirm_on_edit() -> None:
    # The preview card's inline edit sends changed slot values — the pending confirm
    # (and its block id) are stale and must be dropped so the gate re-confirms.
    state = {
        "filled": {"name": "Old"},
        "confirmed": False,
        "confirm_rid": "r1",
        "preview_tried": True,
        "preview_shown": True,
    }
    _record_slot_answer(state, {"name": "My first Agent"})
    assert state["filled"]["name"] == "My first Agent"
    assert "confirm_rid" not in state and "preview_shown" not in state
    assert state["preview_tried"] is True  # content unchanged → keep the sample
    _record_slot_answer(state, {"content": "a new angle"})
    assert "preview_tried" not in state  # content changed → regenerate the dry-run


def test_outlook_recipient_required_when_wired() -> None:
    filled = {"content": "sections", "schedule": "daily 9am"}
    slot = bs.next_missing_slot(bs.SCHEDULED_PUBLISHER, filled, ["outlook-mail"])
    assert slot and slot.name == "outlook_to" and slot.destination_for == "outlook-mail"
    assert bs.classify_agent_type("send updates", ["outlook-mail"]) == bs.SCHEDULED_PUBLISHER
    assert bs.extract_correction("outlook_to", "send it to me at boss@corp.om") == "boss@corp.om"


def test_teams_channel_link_parses_to_team_and_channel() -> None:
    link = (
        "https://teams.microsoft.com/l/channel/19%3Aabc123%40thread.tacv2/General"
        "?groupId=12345678-90ab-cdef-1234-567890abcdef&tenantId=x"
    )
    assert bs.normalize_destination("teams_channel", link) == (
        "12345678-90ab-cdef-1234-567890abcdef/19:abc123@thread.tacv2"
    )
    slot = bs.next_missing_slot(
        bs.SCHEDULED_PUBLISHER, {"content": "x", "schedule": "daily 9am"}, ["ms-teams"]
    )
    assert slot and slot.name == "teams_channel"
    assert bs.classify_agent_type("share updates", ["ms-teams"]) == bs.SCHEDULED_PUBLISHER


def test_normalize_destination_chat_space_forms() -> None:
    # Web URL, resource name, and bare id all normalize to `spaces/<id>`.
    assert (
        bs.normalize_destination("chat_space", "https://chat.google.com/room/AAAAxyz1")
        == "spaces/AAAAxyz1"
    )
    assert bs.normalize_destination("chat_space", "spaces/AAAAxyz1") == "spaces/AAAAxyz1"
    assert bs.normalize_destination("chat_space", "AAAAxyz1") == "spaces/AAAAxyz1"


# --- the gate --------------------------------------------------------------


# kb_id=None in these gate tests → the confirm-step preview (an LLM call) is skipped.
_M = "qwen2.5:7b-instruct"


async def test_gate_asks_all_slots_in_one_sheet() -> None:
    ctx = _ctx(build_state={"docs_asked": True})  # past the docs step — slots under test
    g = await _requirements_gate(ctx, "Helpdesk", "answer IT questions from docs", [], None, _M)
    assert g is not None and g.ui["command"] == "ask_user"
    ids = [q["id"] for q in g.ui["questions"]]
    assert ids[0] == "out_of_scope"  # first must_ask slot leads
    assert "escalation" in ids  # ALL knowledge must_ask slots in ONE sheet (not one-by-one)
    assert ctx.build_state["agent_type"] == bs.KNOWLEDGE


async def test_gate_blocks_build_until_confirmed_then_passes() -> None:
    state: dict = {"docs_asked": True}  # past the docs step — the confirm is under test
    ctx = _ctx(build_state=state)
    g1 = await _requirements_gate(ctx, "HR", "answer HR questions from the handbook", [], None, _M)
    ids = [q["id"] for q in g1.ui["questions"]]
    assert "out_of_scope" in ids and "escalation" in ids  # everything at once
    # Answer them all in one response.
    _record_slot_answer(
        state, {"out_of_scope": "Say it doesn't know", "escalation": "Legal / compliance"}
    )
    # All must_ask filled → the gate now asks for CONFIRMATION (not a build).
    g3 = await _requirements_gate(ctx, "HR", "answer HR questions", [], None, _M)
    assert g3.ui["questions"][0]["id"] == "__confirm__"
    # Decline → the FREEFORM change question in chat (no options menu, no sheet) —
    # never the same confirm looping.
    _record_slot_answer(state, {"__confirm__": "Let me change something"})
    assert state["confirmed"] is False
    g4 = await _requirements_gate(ctx, "HR", "answer HR questions", [], None, _M)
    assert g4.ui["questions"][0]["id"] == "__change__"
    assert g4.ui["freeform"] is True
    assert g4.ui["questions"][0]["options"] == []
    # Naming a slot → exactly that slot is re-asked (its own widget/options).
    _record_slot_answer(state, {"__change__": "Escalation"})
    g5 = await _requirements_gate(ctx, "HR", "answer HR questions", [], None, _M)
    assert [q["id"] for q in g5.ui["questions"]] == ["escalation"]
    _record_slot_answer(state, {"escalation": "Nothing — answer everything"})
    # Back to a FRESH confirm, then accept → the gate finally opens.
    g6 = await _requirements_gate(ctx, "HR", "answer HR questions", [], None, _M)
    assert g6.ui["questions"][0]["id"] == "__confirm__"
    _record_slot_answer(state, {"__confirm__": "Yes, build it"})
    assert state["confirmed"] is True
    assert await _requirements_gate(ctx, "HR", "answer HR questions", [], None, _M) is None


async def test_gate_requires_slack_channel_before_confirm() -> None:
    state: dict = {"agent_type": bs.SCHEDULED_PUBLISHER, "filled": {}, "docs_asked": True}
    ctx = _ctx(build_state=state)
    # Fill content + schedule; with slack wired the gate must still demand the channel.
    state["filled"] = {"content": "newly-added docs", "schedule": "weekdays 9am"}
    g = await _requirements_gate(ctx, "Digest", "post daily to slack", ["slack"], None, _M)
    ids = [q["id"] for q in g.ui["questions"]]
    assert "destination" in ids  # the Slack channel — capability ≠ configuration


def test_picker_captured_skips_only_picker_connectors(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    # Slack's channel comes from the Connect resource picker → its typed slot is
    # skipped. Gmail/Sheets have NO picker — their slot must survive the skip set.
    from sila.core.config import settings
    from sila.services.sila.tool_registry import _picker_captured

    monkeypatch.setattr(settings, "nango_secret_key", "k")
    assert _picker_captured(["slack", "gmail", "google-sheets", "email"]) == {"slack"}
    # Nango unprovisioned → nothing is picker-captured (all fall to typed slots).
    monkeypatch.setattr(settings, "nango_secret_key", "")
    assert _picker_captured(["slack"]) == set()


async def test_gate_asks_gmail_recipient_and_sheet_even_when_oauth_wired(
    monkeypatch,  # type: ignore[no-untyped-def]
) -> None:
    # Regression guard: gmail/sheets are OAuth connectors, but their destination is
    # NOT captured by the Connect flow — the gate must still ask the interview slot
    # (the old skip-all-OAuth logic would have silently skipped it).
    from sila.core.config import settings

    monkeypatch.setattr(settings, "nango_secret_key", "k")
    state: dict = {
        "agent_type": bs.SCHEDULED_PUBLISHER,
        "docs_asked": True,
        "filled": {"content": "a daily digest of my docs", "schedule": "0 9 * * *"},
    }
    ctx = _ctx(build_state=state)
    g = await _requirements_gate(
        ctx, "Digest", "email me a daily digest", ["gmail", "google-sheets"], None, _M
    )
    by_id = {q["id"]: q for q in g.ui["questions"]}
    assert "gmail_to" in by_id and "sheet_id" in by_id
    # Destinations can never be guessed — no "Decide for me" on them.
    assert bs.DECIDE_OPTION not in by_id["gmail_to"]["options"]
    assert bs.DECIDE_OPTION not in by_id["sheet_id"]["options"]


def test_preview_destination_labels_google_targets() -> None:
    from sila.services.sila.tool_registry import _preview_destination

    assert _preview_destination({"gmail_to": "boss@example.com"}) == "boss@example.com"
    label = _preview_destination(
        {"sheet_id": "https://docs.google.com/spreadsheets/d/1234567890ABCDEF/edit"}
    )
    assert label == "Google Sheet 1234567890AB…"
    assert _preview_destination({"sheet_id": "shortid"}) == "Google Sheet shortid"


async def test_worker_gate_fails_closed_without_confirmed_spec() -> None:
    # An autonomous fan-out worker can't pause, so it can never run the interview —
    # it must REFUSE to build ("I didn't ask it to create an agent"), never build
    # on guesses.
    ctx = _ctx(interactive=False)
    gate = await _requirements_gate(ctx, "X", "post daily to slack", ["slack"], None, _M)
    assert gate is not None and gate.ui is None  # a refusal, not a pause
    assert "blocked" in gate.content.lower()


async def test_worker_gate_opens_only_for_complete_confirmed_spec() -> None:
    state = {
        "agent_type": bs.SCHEDULED_PUBLISHER,
        "filled": {"content": "new docs", "schedule": "0 9 * * 1-5", "destination": "#gen"},
        "confirmed": True,
    }
    ctx = _ctx(interactive=False, build_state=state)
    assert await _requirements_gate(ctx, "X", "post daily to slack", ["slack"], None, _M) is None


# --- decide-for-me / delegated answers --------------------------------------


async def test_delegated_answers_resolve_never_parroted() -> None:
    # "steps: idk" must never reach the confirm — it resolves to a derived value.
    state = {"agent_type": bs.WORKFLOW, "filled": {"steps": "idk"}}
    ctx = _ctx(build_state=state)
    gate = await _requirements_gate(ctx, "X", "collect weekly numbers", [], None, _M)
    assert state["filled"]["steps"].startswith("derive the steps from the goal")
    assert gate is not None and "idk" not in str(gate.ui)


def test_delegated_destination_is_reasked_not_guessed() -> None:
    filled = {"content": "sections", "schedule": "weekdays 9am", "destination": "you decide"}
    bs.resolve_undecided(bs.SCHEDULED_PUBLISHER, filled, "post daily")
    assert "destination" not in filled  # deleted → the gate re-asks it


def test_decide_default_prefers_safest_option() -> None:
    filled = {"out_of_scope": "Decide for me", "content": "up to you"}
    bs.resolve_undecided(bs.KNOWLEDGE, filled, "answer HR questions")
    assert filled["out_of_scope"] == "Say it doesn't know and point to a human"


async def test_sheet_offers_decide_for_me_except_destinations() -> None:
    state: dict = {"agent_type": bs.SCHEDULED_PUBLISHER, "filled": {}, "docs_asked": True}
    ctx = _ctx(build_state=state)
    g = await _requirements_gate(ctx, "Digest", "post daily to slack", ["slack"], None, _M)
    by_id = {q["id"]: q for q in g.ui["questions"]}
    assert bs.DECIDE_OPTION in by_id["content"]["options"]
    assert bs.DECIDE_OPTION in by_id["schedule"]["options"]
    assert bs.DECIDE_OPTION not in by_id["destination"]["options"]  # can't be guessed


# --- "Let me change something" → slots corrections ---------------------------


def test_merge_slot_overrides_whitelists_and_reports_change() -> None:
    state = {"filled": {"schedule": "0 9 * * 1-5"}}
    changed = bs.merge_slot_overrides(
        state,
        bs.SCHEDULED_PUBLISHER,
        {"schedule": "0 17 * * 1-5", "evil_key": "x", "content": ""},
    )
    assert changed is True
    assert state["filled"]["schedule"] == "0 17 * * 1-5"
    assert "evil_key" not in state["filled"]


def test_merge_slot_overrides_noop_on_same_or_garbage() -> None:
    state = {"filled": {"schedule": "0 9 * * 1-5"}}
    same = bs.merge_slot_overrides(state, bs.SCHEDULED_PUBLISHER, {"schedule": "0 9 * * 1-5"})
    assert same is False
    assert bs.merge_slot_overrides(state, bs.SCHEDULED_PUBLISHER, "garbage") is False


def test_confirm_decline_sets_needs_change() -> None:
    state: dict = {}
    _record_slot_answer(state, {"__confirm__": "Let me change something"})
    assert state["confirmed"] is False
    assert state["needs_change"] is True
    _record_slot_answer(state, {"__confirm__": "Yes, build it"})
    assert state["confirmed"] is True


# --- recorder --------------------------------------------------------------


def test_record_slot_answer_writes_filled_and_clears_asked() -> None:
    state = {"asked": "content", "filled": {}}
    _record_slot_answer(state, {"content": ["A different section each day"]})
    assert state["filled"]["content"] == "A different section each day"
    assert state["asked"] is None


def test_record_slot_answer_records_all_by_key() -> None:
    # Batched answers are recorded by question id (= slot name) — a single response now
    # carries several, so every key is written (no single `asked` needed).
    state: dict = {"filled": {}}
    _record_slot_answer(state, {"content": "x", "schedule": "weekdays 9am"})
    assert state["filled"] == {"content": "x", "schedule": "weekdays 9am"}


# --- model validation (the LLM hallucinated 'text-adalm-3b' into the build) --------


async def test_validated_model_rejects_hallucinated_name(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from sila.core.config import settings
    from sila.services.sila.tool_registry import _validated_model

    default = settings.sila_llm_model
    # No-network short-circuits.
    assert await _validated_model("") == default
    assert await _validated_model(default) == default
    # A hallucinated name with Ollama unreachable → safe default, never the hallucination.
    monkeypatch.setattr(settings, "ollama_url", "http://127.0.0.1:1")
    assert await _validated_model("text-adalm-3b") == default


# --- run capability (the n8n→Sila agent-run token) --------------------------


async def test_build_refuses_duplicate_name() -> None:
    # A weak model re-calling build after success must NOT create a second agent.
    from sila.services.sila.tool_registry import _create_agent_workflow

    ctx = _ctx(build_state={"built_names": ["Daily Cyber Summary Poster"]})
    res = await _create_agent_workflow(
        ctx, {"name": "Daily Cyber Summary Poster", "purpose": "post daily to slack"}
    )
    assert res.ui is None  # terminal — no pause, no re-interview
    assert "already built" in res.content.lower()


def test_run_capability_roundtrip_and_tamper() -> None:
    from sila.services.sila import run_capability as rc

    ws, kb = uuid.uuid4(), uuid.uuid4()
    tok = rc.mint(workspace_id=ws, kb_id=kb, model="qwen2.5:7b-instruct")
    cap = rc.verify(tok)
    assert cap and cap.workspace_id == ws and cap.kb_id == kb and cap.model == "qwen2.5:7b-instruct"
    # Tampered / malformed → None (fail-closed).
    assert rc.verify(tok[:-3] + "xxx") is None
    assert rc.verify("garbage") is None
    # A connector-capability token must not verify as a run capability (scope-isolated).
    from sila.services.connectors import capability as cc

    other = cc.mint(
        workspace_id=ws,
        user_id=uuid.uuid4(),
        provider="slack",
        endpoint="chat.postMessage",
        connection_id="c1",
    )
    assert rc.verify(other) is None


# --- the deterministic docs step ---------------------------------------------


async def test_gate_asks_docs_once_for_grounded_types() -> None:
    # Knowledge/publisher agents get the upload panel FIRST (deterministic — no
    # model round needed after an accepted plan), exactly once, and skippable.
    state: dict = {}
    ctx = _ctx(build_state=state)
    g1 = await _requirements_gate(ctx, "HR", "answer HR questions from docs", [], None, _M)
    assert g1 is not None and g1.ui is not None
    assert g1.ui["command"] == "open_modal" and g1.ui["modal"] == "upload"
    # Second pass (user skipped) → on to the requirement questions, not docs again.
    g2 = await _requirements_gate(ctx, "HR", "answer HR questions from docs", [], None, _M)
    assert g2 is not None and g2.ui is not None and g2.ui["command"] == "ask_user"


async def test_gate_skips_docs_when_kb_present_or_type_ungrounded() -> None:
    # A kb already attached → no docs ask.
    ctx = _ctx(build_state={})
    g = await _requirements_gate(ctx, "HR", "answer HR questions", [], "kb-123", _M)
    assert g is not None and g.ui is not None and g.ui["command"] == "ask_user"
    # Monitor/workflow types are not doc-grounded → no docs ask either.
    ctx2 = _ctx(build_state={})
    g2 = await _requirements_gate(ctx2, "W", "run a multi-step pipeline", [], None, _M)
    assert g2 is not None and g2.ui is not None and g2.ui["command"] == "ask_user"


# --- the deterministic change flow -------------------------------------------


async def test_change_free_text_updates_the_slot_and_reconfirms() -> None:
    # "make it weekdays only at 5pm" maps to the schedule slot by keywords, becomes
    # the new value, and the confirm re-reflects it — no model round anywhere.
    state: dict = {
        "agent_type": bs.SCHEDULED_PUBLISHER,
        "docs_asked": True,
        "filled": {"content": "sections", "schedule": "0 9 * * 1-5"},
        "change_slot": "make it weekdays only at 5pm",
        "confirmed": True,
        "preview_shown": True,
    }
    ctx = _ctx(build_state=state)
    g = await _requirements_gate(ctx, "Digest", "post daily summaries", [], None, _M)
    assert state["filled"]["schedule"] == "make it weekdays only at 5pm"
    assert state["confirmed"] is False  # the changed spec must be re-confirmed
    assert "preview_shown" not in state  # the stale preview is invalidated
    assert g is not None and g.ui["questions"][0]["id"] == "__confirm__"


async def test_change_defaultable_slot_is_asked_explicitly() -> None:
    # "Name" is defaultable (never in the missing-slots walk) — picking it from the
    # change menu asks it directly.
    state: dict = {
        "agent_type": bs.SCHEDULED_PUBLISHER,
        "docs_asked": True,
        "filled": {"content": "sections", "schedule": "0 9 * * 1-5"},
        "change_slot": "Name",
        "confirmed": True,
    }
    ctx = _ctx(build_state=state)
    g = await _requirements_gate(ctx, "Digest", "post daily summaries", [], None, _M)
    assert g is not None and g.ui["questions"][0]["id"] == "name"


# --- schedule phrase parsing ---------------------------------------------------


def test_schedule_phrases_parse_time_and_days() -> None:
    from sila.services.sila.tool_registry import _schedule_to_cron

    assert _schedule_to_cron("15 14 * * 1-5") == "15 14 * * 1-5"  # picker passthrough
    assert _schedule_to_cron("make it weekdays only at 5pm") == "0 17 * * 1-5"
    assert _schedule_to_cron("every day at 7:30") == "30 7 * * *"
    assert _schedule_to_cron("Weekly on Monday at 9:00 AM") == "0 9 * * 1"
    assert _schedule_to_cron("Every weekday at 12pm") == "0 12 * * 1-5"
    assert _schedule_to_cron("daily") == "0 9 * * *"  # no time given → 9am default


# --- goal pinning ---------------------------------------------------------------


async def test_goal_pins_purpose_over_model_paraphrase() -> None:
    # The user asked for an HR Q&A agent; the model paraphrased it into the prompt's
    # daily-Slack example. The pinned goal wins → correct classification + purpose.
    from sila.services.sila.tool_registry import _create_agent_workflow

    ctx = _ctx(build_state={"goal": "Build an agent that answers questions from my HR handbook"})
    res = await _create_agent_workflow(
        ctx, {"name": "X", "purpose": "posts daily summaries to Slack"}
    )
    assert ctx.build_state["build_args"]["purpose"].endswith("HR handbook")
    assert ctx.build_state["agent_type"] == bs.KNOWLEDGE  # not scheduled_publisher
    assert res.ui is not None and res.ui["command"] == "open_modal"  # docs first


# ---------------------------------------------------------------------------
# LLM-personalised interview wording — the validation gate (_valid_phrases)
# ---------------------------------------------------------------------------


def test_valid_phrases_filters_unsafe_output() -> None:
    from sila.services.sila.build_slots import Slot
    from sila.services.sila.tool_registry import _valid_phrases

    slots = [
        Slot("content", "What should each post contain?"),
        Slot("schedule", "When should it run?"),
    ]
    raw = {
        "content": "For your daily digest, what should each edition include?",  # ok
        "schedule": "Run it whenever.",  # not a question -> fallback
        "evil_slot": "Which password should it use?",  # unknown id -> dropped
        "extra": 123,  # non-string -> dropped
    }
    out = _valid_phrases(slots, raw)
    assert out == {"content": "For your daily digest, what should each edition include?"}


def test_valid_phrases_rejects_non_dict_and_lengths() -> None:
    from sila.services.sila.build_slots import Slot
    from sila.services.sila.tool_registry import _valid_phrases

    slots = [Slot("content", "What should each post contain?")]
    assert _valid_phrases(slots, ["not", "a", "dict"]) == {}
    assert _valid_phrases(slots, {"content": "Hm?"}) == {}  # too short
    assert _valid_phrases(slots, {"content": ("x" * 300) + "?"}) == {}  # too long


def test_slots_ask_uses_phrased_with_canonical_fallback() -> None:
    from sila.services.sila.build_slots import Slot
    from sila.services.sila.tool_registry import _slots_ask

    slots = [
        Slot("content", "What should each post contain?"),
        Slot("schedule", "When should it run?"),
    ]
    res = _slots_ask(slots, lead="lead", phrased={"content": "What goes in each digest?"})
    qs = {q["id"]: q["question"] for q in res.ui["questions"]}
    assert qs["content"] == "What goes in each digest?"  # personalised
    assert qs["schedule"] == "When should it run?"  # canonical fallback
