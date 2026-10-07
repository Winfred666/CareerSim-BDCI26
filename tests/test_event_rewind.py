"""Exact observe recovery, stale instruction removal and fail-closed retries."""

import json
from argparse import Namespace
from unittest.mock import AsyncMock

import pytest

from career_sim_runner.coach.history import legacy_boundary, observe_boundary, retained_history
from career_sim_runner.coach import cli as coach


def tc(key, name="observe", **args):
    return {"role": "assistant", "event_type": "chat.tool_call",
            "tool_call": {"name": name, "tool_call_id": key, "arguments": args}}


def tr(key, result="done"):
    return {"role": "assistant", "event_type": "chat.tool_result", "tool_call_id": key, "result": result}


def test_same_turn_same_month_repeated_observe_and_parallel_pairs():
    records = [{"role": "user", "content": "play"}, tc("rules", "read_file", file_path="skills/x/SKILL.md"),
               tr("rules", "obsolete rules"), tc("stage", "bash", command="cat skills/x/stages/decision.md"),
               tr("stage", "obsolete stage"), {"event_type": "context.compact_summary", "content": "old summary"}]
    for event in range(7):
        records += [tc(f"o{event}"), tr(f"o{event}", f"/snapshots/{event}.json"),
                    tc(f"repeat{event}"), tr(f"repeat{event}"),
                    tc(f"a{event}", "take_action", choice=1), tr(f"a{event}")]
    for event in range(7):
        loc = observe_boundary(records, {"observe_json_path": f"/snapshots/{event}.json"})
        assert loc["tool_call_id"] == f"o{event}"
        kept = retained_history(records, f"o{event}")
        assert len([r for r in kept if r.get("tool_call", {}).get("name") == "take_action"]) == event
        assert "obsolete" not in json.dumps(kept)
        assert "summary" not in json.dumps(kept)
    records = [tc("done", "read_file"), tc("parallel", "read_file"), tr("done"), tc("target"),
               tr("parallel"), tr("target")]
    assert retained_history(records, "target") == [records[0], records[2]]


def test_legacy_requires_unique_raw_action_evidence():
    checkpoint = {"captured_at": "2026-01-01T00:00:00+00:00", "session_id": "game",
                  "decision": {"choice": {"number": 1, "notes": "decision"}}}
    records = [tc("first", session_id="game"), tr("first"), tc("repeated", session_id="game"),
               tr("repeated"), {**tc("a", "take_action", session_id="game", choice=1, notes="decision"),
                               "timestamp": 1767225599}]
    assert legacy_boundary(records, checkpoint)["tool_call_id"] == "first"
    with pytest.raises(RuntimeError, match="ambiguous"):
        legacy_boundary(records + [records[-1]], checkpoint)
    with pytest.raises(RuntimeError, match="evidence"):
        legacy_boundary([], {**checkpoint, "captured_at": "unknown"})


@pytest.mark.parametrize("wrap", [lambda value: value, json.dumps,
    lambda value: {"content": [{"text": json.dumps(value)}]}])
def test_indirect_rule_payloads_are_removed_with_their_tool_calls(wrap):
    rules = {"translation_rules": "obsolete translation",
             "decision_policy": "obsolete policy", "review_rules": "obsolete review"}
    records = [tc("observe"), tr("observe", {"status": {"level": "L4"}}),
               tc("context", "bash", command="python scripts/read-context.py"),
               tr("context", wrap(rules)),
               tc("tagged", "bash", command="python helper.py"),
               tr("tagged", "<installed_rule>obsolete inline</installed_rule>"),
               tc("action", "take_action", choice=1), tr("action", "success"),
               tc("ordinary", "read_file", file_path="notebooks/facts.tsv"),
               tr("ordinary", "decision_policy is discussed, but no rule payload"),
               tc("boundary")]
    kept = retained_history(records, "boundary")
    assert kept == records[:2] + records[6:10]
    assert "obsolete" not in json.dumps(kept)
    assert {r["tool_call"]["tool_call_id"] for r in kept if "tool_call" in r} == {
        r["tool_call_id"] for r in kept if r["event_type"] == "chat.tool_result"}


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["agent", "team"])
@pytest.mark.parametrize("failure", ["context", "simulator", "reload"])
async def test_pending_withdraw_retry_preserves_identity_and_cannot_execute(tmp_path, monkeypatch, mode, failure):
    from career_emulator.server.models import GameSession
    from career_emulator.storage import SessionStore
    db = tmp_path / "game.db"
    before = GameSession(session_id="game").to_dict()
    after = GameSession(session_id="game")
    after.career.current_month = 2
    await SessionStore(db, tmp_path / "logs").save_session(after)
    coach._register_coach_session(db, "player", "game", "player", "")
    monkeypatch.setattr(coach, "_coach_emulator_log_dir", lambda _: tmp_path / "logs")
    monkeypatch.setattr(coach, "resolve_instance_ws_url", lambda: "ws://unused")
    monkeypatch.setattr(coach, "cancel_agent_session", AsyncMock(return_value={"success": True}))
    monkeypatch.setattr(coach, "read_history", lambda *_: [tc("first")])
    root = tmp_path / "skills"
    notebooks = root / "x" / "notebooks"
    notebooks.mkdir(parents=True)
    (notebooks / "state").write_bytes(b"original\x00\xff")
    snapshot = coach.capture_notebooks(root)
    (notebooks / "future").write_text("future")
    monkeypatch.setattr(coach, "_runtime_skills_dir", lambda _: root)
    gate = coach.Gate(coach.gate_path(db, "player"))
    gate.create("game", mode=mode, agent_session_id="player")
    checkpoint = coach.Checkpoint("game", "now", before, [], coach._json_hash(after.to_dict()), "player",
                                  before_notebooks=snapshot, observe_boundary={"tool_call_id": "first"})
    store = coach.CheckpointStore(db, "game")
    store.append(checkpoint)
    success = {"rewind_context": True, "context_persisted": True, "history_persisted": True, "team_reset": mode == "team"}
    rewind = AsyncMock(side_effect=[{**success, "context_persisted": False}, success] if failure == "context" else None,
                       return_value=success)
    monkeypatch.setattr(coach, "rewind_agent_session", rewind)
    original_replace = coach._replace_session
    attempts = 0
    async def replace(*args):
        nonlocal attempts
        attempts += 1
        result = await original_replace(*args)
        if failure == "simulator" and attempts == 1:
            raise OSError("injected simulator failure after commit")
        return result
    monkeypatch.setattr(coach, "_replace_session", replace)
    reload = AsyncMock(side_effect=[RuntimeError("injected reload failure"), {"reloaded": True, "agent_session_id": "player"}]
                       if failure == "reload" else None, return_value={"reloaded": True, "agent_session_id": "player"})
    monkeypatch.setattr(coach, "_reload_solution", reload)
    args = Namespace(db=str(db), session_id="player", checkpoint_round=0)
    with pytest.raises((OSError, RuntimeError)):
        await coach._withdraw(args)
    assert len(store.records()) == 1
    with pytest.raises(RuntimeError):
        gate.grant()
    with pytest.raises(RuntimeError, match="pending"):
        coach._require_no_pending_withdraw(db, "player")
    result = await coach._withdraw(args)
    assert result["withdrawn"] and result["agent_session_id"] == "player" and result["game_session_id"] == "game"
    assert await coach.read_session_payload(db, "game") == before
    assert coach.capture_notebooks(root) == snapshot
    assert not store.records()
    assert rewind.await_count == (2 if failure == "context" else 1)
    if failure == "context":
        assert rewind.await_args_list[0].kwargs["operation_id"] == rewind.await_args_list[1].kwargs["operation_id"]


@pytest.mark.asyncio
async def test_persistence_failure_is_propagated_to_rewind_context(monkeypatch):
    from jiuwenswarm.agents.harness.common import session_ops_service as ops
    from types import SimpleNamespace
    session = SimpleNamespace(get_session_id=lambda: "player", update_state=lambda _: None)
    engine = SimpleNamespace(get_context=lambda **_: None, clear_context=AsyncMock(), create_context=AsyncMock())
    deep = SimpleNamespace(_interaction_session=session)
    monkeypatch.setattr(ops, "_persist_rewound_session", AsyncMock(return_value=False))
    result = await ops._apply_rewound_context(deep_agent=deep, react_agent=SimpleNamespace(context_engine=engine),
                                             session_id="player", turn_index=0, context_messages=[], skipped=0)
    assert result is False


@pytest.mark.asyncio
@pytest.mark.parametrize("team", [False, True])
async def test_server_rewind_rebuilds_clean_model_messages_and_retries_save(tmp_path, monkeypatch, team):
    from types import SimpleNamespace
    from career_sim_runner.jiuwen_rewind import rewind_before_tool
    from jiuwenswarm.server.runtime.session import session_history as history
    from jiuwenswarm.agents.harness.common import session_ops_service as ops
    from jiuwenswarm.agents.harness import team as team_module
    monkeypatch.setattr(history, "get_agent_sessions_dir", lambda: tmp_path)
    original = [{"role": "user", "content": "play"}, tc("stage", "read_file", file_path="skills/x/stages/decide.md"),
                tr("stage", "OLD_STAGE_TEXT"), tc("valid", "take_action"), tr("valid"),
                {"role": "assistant", "event_type": "context.compact_summary", "content": "OLD_SUMMARY"},
                tc("target"), tr("target"), tc("future", "take_action"), tr("future")]
    history.write_history_records("player", original)
    deep = SimpleNamespace(react_agent=object(), configured_rails=lambda: [])
    server = SimpleNamespace(_session_stream_tasks={}, _resolve_rewind_agent=AsyncMock(return_value=(deep, deep.react_agent)))
    manager = SimpleNamespace(cancel_session_runtime=AsyncMock(), delete_session_runtime=AsyncMock(return_value=True))
    monkeypatch.setattr(team_module, "get_team_manager", lambda: manager)
    apply = AsyncMock(side_effect=[False, True])
    monkeypatch.setattr(ops, "_apply_rewound_context", apply)
    params = {"before_tool_call_id": "target", "operation_id": "one-operation", "reset_team": team}
    with pytest.raises(RuntimeError, match="persistence failed"):
        await rewind_before_tool(server, "web", "player", params)
    # Target is already gone, but retry uses the immutable operation journal.
    assert "target" not in json.dumps(history.load_history_records("player"))
    result = await rewind_before_tool(server, "web", "player", params)
    assert result["context_persisted"] is True and result["team_reset"] is team
    assert manager.delete_session_runtime.await_count == int(team)
    messages = apply.await_args.kwargs["context_messages"]
    text = json.dumps([m.model_dump() for m in messages])
    assert all(value not in text for value in ("OLD_STAGE_TEXT", "OLD_SUMMARY", "target", "future"))
    assert "valid" in text
    assert await rewind_before_tool(server, "web", "player", params) == result
    assert apply.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["rewind_context", "context_persisted", "history_persisted"])
async def test_wire_rewind_rejects_unverified_success(monkeypatch, missing):
    from career_sim_runner import ws_client
    response = {"rewind_context": True, "context_persisted": True, "history_persisted": True}
    response[missing] = False
    request = AsyncMock(return_value=response)
    monkeypatch.setattr(ws_client, "_session_request", request)
    with pytest.raises(RuntimeError):
        await ws_client.rewind_agent_session("ws://unused", "player", before_tool_call_id="observe",
                                             operation_id="operation")
    assert request.await_args.args[3]["before_tool_call_id"] == "observe"


def test_reloaded_rule_delivery_is_removed_on_next_rewind(tmp_path):
    skill = tmp_path / "skill"
    (skill / "stages").mkdir(parents=True)
    (skill / "SKILL.md").write_text("entry current")
    (skill / "stages" / "observe.md").write_text("stage current")
    (skill / "notebooks").mkdir()
    (skill / "notebooks" / "memory.md").write_text("private memory")
    prompt = coach._installed_rules_prompt(skill)
    assert "entry current" in prompt and "stage current" in prompt
    assert "private memory" not in prompt
    user = {"role": "user", "content": [{"type": "text", "text": "continue" + prompt}]}
    kept = retained_history([user, tc("boundary")], "boundary")
    assert "continue" in json.dumps(kept)
    assert "stage current" not in json.dumps(kept)
    assert "entry current" not in json.dumps(kept)
    assert "entry current" in json.dumps(user)
