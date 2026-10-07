"""Tests for the coach-only single-step controls."""

import json
from argparse import Namespace
from pathlib import Path

import pytest

from career_sim_runner.coach import cli as coach


async def _async_true(*_args, **_kwargs) -> bool:
    return True


async def _async_one(*_args, **_kwargs) -> int:
    return 1


async def _async_value(value):
    return value


@pytest.mark.parametrize("flags", [("--context-mode", "isolated-events"), ("--discard-incomplete-event",)])
def test_reload_rejects_removed_event_context_flags(flags) -> None:
    """The retired ablation cannot be enabled through the command line."""
    with pytest.raises(SystemExit) as error:
        coach._parser().parse_args(["reload", "--session-id", "player", *flags])
    assert error.value.code == 2


def test_step_prompt_names_existing_session_and_one_action() -> None:
    """The coach prompt must explicitly bound the agent to one action."""
    prompt = coach._step_prompt("game-123")
    assert "game-123" in prompt
    assert "take_action" not in prompt
    assert len(prompt) < 100


def test_team_step_prompt_requires_fresh_real_delegation() -> None:
    """Every fresh team conversation must rebuild roles before acting."""
    prompt = coach._step_prompt("game-123", team_mode=True)
    assert "build_team" not in prompt
    assert "spawn_teammate" not in prompt
    assert "Leader" not in prompt
    assert "ls -1 <skill_directory>/notebooks/translation-keeps/" not in prompt
    assert "消息键固定为" not in prompt
    assert "skill_directory" not in prompt
    assert "show_employee_handbook" not in prompt
    assert "task_tool" not in prompt


def test_team_step_prompt_does_not_replay_history_into_leader() -> None:
    """Team Leader receives two briefs, not the retained state ledger."""
    prompt = coach._step_prompt(
        "game-123",
        team_mode=True,
        history_records=[{"round": 1, "question": {"event": {"title": "不应进入 Leader"}}}],
    )
    assert "不应进入 Leader" not in prompt


def test_step_prompt_preserves_one_shot_system_feedback() -> None:
    """The pre-step snapshot must not hide consumable HR feedback from Jiuwen."""
    prompt = coach._step_prompt(
        "game-123",
        team_mode=True,
        system_feedback="本月工资到账。HR提醒：最近风险有所上升。",
    )
    assert "HR提醒：最近风险有所上升" not in prompt
    assert "fresh observe.events" not in prompt
    assert "由 Translator" not in prompt


def test_merge_system_feedback_preserves_distinct_fragments() -> None:
    """A consumed warning remains available to the next one-shot team."""
    assert coach._merge_system_feedback("关系好的HR", "关系好的HR", "半年绩效奖金（A）：0.4") == (
        "关系好的HR\n\n半年绩效奖金（A）：0.4"
    )


def test_copy_keeps_notebooks_exports_team_state_without_translations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Coach outputs expose live keeps files without per-event translations."""
    instance = tmp_path / "instance"
    session_id = "player-1"
    team_id = "career-sim-coach_1"
    history = instance / "agent" / "sessions" / session_id / "history.jsonl"
    history.parent.mkdir(parents=True)
    history.write_text(
        '{"event":{"team_id":"career-sim-coach_1"}}\n',
        encoding="utf-8",
    )
    notebooks = instance / ".agent_teams" / team_id / "team-workspace" / "skills" / "skill-a" / "notebooks"
    translations = notebooks / "translation-keeps"
    translations.mkdir(parents=True)
    (notebooks / "state-keeps.md").write_text("state\n", encoding="utf-8")
    (notebooks / "signals-keeps.tsv").write_text("signals\n", encoding="utf-8")
    (notebooks / "dictionary.tsv").write_text("dictionary\n", encoding="utf-8")
    (notebooks / "stat-cap.tsv").write_bytes(b"L\tO\tS\tN\nL2\t6\t?\t?\n")
    (translations / "00001.md").write_text("translation\n", encoding="utf-8")
    monkeypatch.setattr(coach, "jiuwenswarm_data_dir", lambda: instance)

    output = tmp_path / "coach-output"
    copied = coach._copy_keeps_notebooks(session_id, output)

    assert copied == [
        output / "notebooks" / "skill-a" / "signals-keeps.tsv",
        output / "notebooks" / "skill-a" / "stat-cap.tsv",
        output / "notebooks" / "skill-a" / "state-keeps.md",
    ]
    assert (output / "notebooks" / "skill-a" / "state-keeps.md").read_text() == "state\n"
    assert not (output / "notebooks" / "skill-a" / "translation-keeps").exists()
    mirror = output / "notebooks" / "skill-a" / "stat-cap.tsv"
    assert mirror.read_bytes() == (notebooks / "stat-cap.tsv").read_bytes()
    (notebooks / "stat-cap.tsv").write_bytes(b"L\tO\tS\tN\nL2\t6\t21\t?\n")
    coach._copy_keeps_notebooks(session_id, output)
    assert mirror.read_bytes() == (notebooks / "stat-cap.tsv").read_bytes()
    assert not (output / "notebooks" / "skill-a" / "dictionary.tsv").exists()


def test_coach_log_dir_uses_submission_output_and_migrates_legacy_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One coach game keeps its cumulative emulator log beside submission outputs."""
    from career_sim_runner.models import InstallRecord

    db_path = tmp_path / "runtime" / "game.sqlite3"
    legacy_dir = tmp_path / "runtime" / "emulator_logs"
    output_dir = tmp_path / "runtime" / "outputs" / "winfred_v2" / "20260916T120000Z"
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "game-123.log").write_text("legacy evidence\n", encoding="utf-8")
    monkeypatch.setattr(coach, "default_emulator_log_dir", lambda: legacy_dir)
    monkeypatch.setattr(coach, "timestamped_output_dir", lambda _label: output_dir)
    monkeypatch.setattr(
        coach,
        "load_active_install",
        lambda: InstallRecord("", "winfred_v2", "", "", {"team": "winfred_v2"}),
    )

    resolved, created = coach._ensure_coach_emulator_log_dir(db_path, game_session_id="game-123")

    assert created is True
    assert resolved == output_dir
    assert (output_dir / "game-123.log").read_text(encoding="utf-8") == "legacy evidence\n"
    assert coach._coach_emulator_log_dir(db_path) == output_dir
    assert coach._ensure_coach_emulator_log_dir(db_path, game_session_id="game-123") == (output_dir, False)


def test_full_step_timeout_is_not_retried() -> None:
    """A 600-second team timeout is already the entire event budget."""
    assert "timeout" not in coach.RETRYABLE_STEP_TERMINATIONS


def test_auto_postmortem_is_explicit_and_unlimited_by_default() -> None:
    """The default workflow stays interactive unless the dedicated command is selected."""
    args = coach._parser().parse_args(["auto-postmortem", "--solution-keyword", "mostroles"])

    assert args.command == "auto-postmortem"
    assert args.session_id == ""
    assert args.timeout_s == float("inf")
    assert not hasattr(args, "max_runtime_s")
    assert not hasattr(args, "max_events")


@pytest.mark.parametrize(
    ("observation", "outcome"),
    [
        (
            {
                "current_state": {"simulation_flags": {"alive": True, "failed": False}},
                "ending_score": {"outcome": "completed", "completed": True, "survival_months": 48},
            },
            "completed",
        ),
        (
            {
                "current_state": {"simulation_flags": {"alive": False, "failed": True}},
                "ending_score": {"outcome": "eliminated", "completed": False, "survival_months": 11},
            },
            "eliminated",
        ),
        (
            {
                "current_state": {"simulation_flags": {"alive": True, "failed": False}},
                "ending_score": {"outcome": "completed", "completed": True, "survival_months": 47},
            },
            None,
        ),
    ],
)
def test_verified_terminal_outcome_requires_structured_ending(observation: dict, outcome: str | None) -> None:
    """Agent prose and partial endings cannot terminate an automatic run."""
    assert coach._verified_terminal_outcome(observation) == outcome


@pytest.mark.asyncio
async def test_auto_postmortem_runs_steps_until_terminal_and_preserves_seed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One automatic invocation advances without an external coach turn between events."""
    calls: list[tuple[bool, str, str]] = []
    observations = iter(
        [
            {
                "current_state": {"simulation_flags": {"alive": True, "failed": False}},
                "ending_score": None,
            },
            {
                "current_state": {"simulation_flags": {"alive": True, "failed": False}},
                "ending_score": {
                    "outcome": "completed",
                    "completed": True,
                    "survival_months": 48,
                    "quantitative_score": 123.0,
                },
            },
        ]
    )

    async def fake_step(args: Namespace, *, start: bool = False) -> dict:
        calls.append((start, getattr(args, "session_id", ""), getattr(args, "seed", "")))
        number = len(calls)
        return {
            "ok": True,
            "jiuwen_player_session_id": "player-1",
            "session_id": "player-1",
            "game_session_id": "game-1",
            "seed": f"game-1:{number}",
            "inspection": {},
        }

    async def fake_peek(*_args, **_kwargs) -> dict:
        return {"observation": next(observations)}

    monkeypatch.setattr(coach, "_run_step", fake_step)
    monkeypatch.setattr(coach, "_peek", fake_peek)
    args = coach._parser().parse_args(["auto-postmortem", "--solution-keyword", "mostroles"])

    result = await coach._run_auto_postmortem(args)

    assert calls == [(True, "", ""), (False, "player-1", "game-1:1")]
    assert result["ok"] is True
    assert result["terminal"] is True
    assert result["terminal_outcome"] == "completed"
    assert result["postmortem_ready"] is True
    assert result["events_completed"] == 2
    assert result["ending_score"]["quantitative_score"] == 123.0


@pytest.mark.asyncio
async def test_auto_postmortem_returns_when_player_exits_before_action(monkeypatch: pytest.MonkeyPatch) -> None:
    """Automatic mode never hides a failed event behind an implicit retry."""
    calls = 0

    async def failed_step(_args: Namespace, *, start: bool = False) -> dict:
        nonlocal calls
        calls += 1
        return {"ok": False, "termination_reason": "agent_stopped_before_action", "inspection": {}}

    monkeypatch.setattr(coach, "_run_step", failed_step)
    args = coach._parser().parse_args(["auto-postmortem"])

    result = await coach._run_auto_postmortem(args)

    assert calls == 1
    assert result["ok"] is False
    assert result["termination_reason"] == "agent_stopped_before_action"
    assert result["terminal"] is False
    assert result["events_completed"] == 0


@pytest.mark.asyncio
@pytest.mark.parametrize('rotation_fails', [False, True])
async def test_auto_postmortem_fresh_events_rotate_after_enrollment_and_initialization(monkeypatch, rotation_fails):
    from career_sim_runner.coach import fresh_loop

    calls = []

    async def step(args, *, start=False):
        calls.append(('step', start, args.seed))
        return {'ok': True, 'jiuwen_player_session_id': 'player', 'game_session_id': 'game',
                'seed': f'fixed:{len(calls)}'}

    observations = 0

    async def peek(*_):
        nonlocal observations
        observations += 1
        return {'observation': {'ending_score': None if observations < 3 else {
            'outcome': 'completed', 'completed': True, 'survival_months': 48},
            'current_state': {'simulation_flags': {'alive': True, 'failed': False}}}}

    async def rotate(db, player, ws):
        calls.append(('rotate', player, ws))
        if rotation_fails:
            raise RuntimeError('rotation infrastructure failure')

    monkeypatch.setattr(coach, '_run_step', step)
    monkeypatch.setattr(coach, '_peek', peek)
    monkeypatch.setattr(fresh_loop, 'next_event', rotate)
    args = coach._parser().parse_args(['auto-postmortem', '--fresh-events', '--mode', 'team',
                                      '--ws-url', 'ws://test'])
    result = await coach._run_auto_postmortem(args)
    assert calls[:3] == [('step', True, ''), ('step', False, 'fixed:1'),
                         ('rotate', 'player', 'ws://test')]
    if rotation_fails:
        assert len(calls) == 3
        assert not result['terminal'] and not result['ok']
        assert result['termination_reason'] == 'context_rotation_failed'
        assert result['events_completed'] == 2
    else:
        assert calls[3] == ('step', False, 'fixed:2')
        assert result['terminal'] and result['events_completed'] == 3


def test_fresh_step_prompt_injects_retained_history_only() -> None:
    """A fresh session receives retained decisions while withdrawn ones stay absent."""
    prompt = coach._step_prompt(
        "game-123",
        "game-123:4",
        history_records=[
            {
                "round": 1,
                "question": {"event": {"title": "事件一"}, "choices": [{"choice": 1, "action": "选项一"}]},
                "decision": {"choice": {"number": 1, "action": "选项一", "notes": "理由一"}},
                "consequence": {"changed": {"health": {"before": 5, "after": 4}}},
                "state_after": {"health": 4},
                "next_question": {},
            },
            {
                "round": 3,
                "question": {"event": {"title": "事件三"}},
                "decision": {"choice": {"number": 2, "action": "选项三", "notes": "理由三"}},
                "consequence": {"changed": {}},
                "state_after": {"health": 4},
                "next_question": {},
            },
        ],
    )
    assert "事件一" not in prompt
    assert "事件三" not in prompt
    assert "game-123:4" not in prompt


def test_event_seed_matches_emulator_monthly_sampler() -> None:
    """The coach seed must use session id and current month exactly."""
    assert coach._event_seed("game-123", {"career": {"current_month": 7}}) == "game-123:7"


def test_checkpoint_store_pops_only_latest(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Withdrawal should consume exactly one checkpoint and retain older history."""
    monkeypatch.setattr(coach, "_history_path", lambda _db, _sid: tmp_path / "history.jsonl")
    store = coach.CheckpointStore(tmp_path / "game.sqlite3", "game-123")
    first = coach.Checkpoint("game-123", "one", {"n": 1}, [], "before-1", "drive")
    second = coach.Checkpoint("game-123", "two", {"n": 2}, [], "before-2", "drive")
    store.append(first)
    store.append(second)

    assert store.pop() == second
    assert store.pop() == first
    assert store.pop() is None


def test_checkpoint_store_supports_incremental_arbitrary_withdrawal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A single ledger can replay state and restore any earlier round."""
    monkeypatch.setattr(coach, "_history_path", lambda _db, _sid: tmp_path / "ledger.json")
    store = coach.CheckpointStore(tmp_path / "game.sqlite3", "game-123")
    first_before = {"career": {"current_month": 1, "health": 5}}
    first_after = {"career": {"current_month": 2, "health": 4}}
    second_after = {"career": {"current_month": 3, "health": 4}}
    store.append(
        coach.Checkpoint(
            "game-123", "one", first_before, [], coach._json_hash(first_after), "drive", seed="game-123:1",
            payload_delta=coach._payload_delta(first_before, first_after),
        )
    )
    store.append(
        coach.Checkpoint(
            "game-123", "two", None, [], coach._json_hash(second_after), "drive", seed="game-123:2",
            payload_delta=coach._payload_delta(first_after, second_after),
        )
    )
    assert store.records()[1]["before_payload"] == first_after
    restored = store.withdraw(1)
    assert restored is not None and restored.before_payload == first_before
    assert store.records() == []


@pytest.mark.parametrize("command", ["step", "inspect", "withdraw", "reload"])
def test_coach_commands_require_explicit_player_session_id(command: str) -> None:
    """Every operation on an existing run must identify the Jiuwen player session."""
    with pytest.raises(SystemExit):
        coach._parser().parse_args([command])


def test_inspect_is_the_only_coach_query_command() -> None:
    """Inspect accepts progressive disclosure ranges and old aliases are gone."""
    args = coach._parser().parse_args(["inspect", "--session-id", "coach-1", "-i", "2", "-j", "4"])
    assert args.command == "inspect"
    assert args.from_round == 2
    assert args.to_round == 4
    canonical = coach._parser().parse_args(
        ["inspect", "--jiuwen-player-session-id", "player-1", "-i", "2", "-j", "4"]
    )
    assert canonical.session_id == "player-1"
    for removed in ("status", "peek"):
        with pytest.raises(SystemExit):
            coach._parser().parse_args([removed, "--session-id", "coach-1"])
    with pytest.raises(SystemExit):
        coach._parser().parse_args(
            ["withdraw", "--session-id", "coach-1", "--reload-only"]
        )


def test_select_inspection_records_defaults_to_latest() -> None:
    records = [{"round": 1}, {"round": 2}, {"round": 3}]
    assert coach._select_inspection_records(records, start_round=None, end_round=None) == [{"round": 3}]
    assert coach._select_inspection_records(records, start_round=2, end_round=3) == records[1:]
    assert coach._select_inspection_records(records, start_round=2, end_round=None) == records[1:]


@pytest.mark.asyncio
@pytest.mark.parametrize("run_mode", ["agent", "team"])
@pytest.mark.parametrize("recovery", ["preserve", "rewound", "replace"])
@pytest.mark.parametrize("fail_install", [False, True])
async def test_reload_restores_same_game_and_preserves_agent_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, recovery: str, fail_install: bool, run_mode: str
) -> None:
    """Reload preserves game and notebooks across supported session recovery modes."""
    reuse_rewound = recovery == "rewound"
    replace_player = recovery == "replace"
    from career_emulator.server.models import GameSession
    from career_sim_runner.models import InstallRecord
    from career_emulator.storage import SessionStore

    db_path = tmp_path / "game.sqlite3"
    log_dir = tmp_path / "logs"
    solution_dir = tmp_path / "solution"
    solution_dir.mkdir()
    (solution_dir / "manifest.json").write_text(json.dumps({"team": "test", "mode": run_mode}), encoding="utf-8")
    session = GameSession(session_id="game-123")
    session.career.current_month = 7
    store = SessionStore(db_path, log_dir)
    await store.save_session(session)
    monkeypatch.setattr(coach, "default_emulator_log_dir", lambda: log_dir)
    monkeypatch.setattr(coach, "ensure_runtime_dirs", lambda: None)
    monkeypatch.setattr(coach, "resolve_instance_ws_url", lambda: "ws://coach")
    monkeypatch.setattr(
        coach,
        "_ensure_coach_emulator_log_dir",
        lambda *_args, **_kwargs: (log_dir, False),
    )
    monkeypatch.setattr(coach, "ensure_instance_configured", lambda **_kwargs: solution_dir)
    runtime = tmp_path / "runtime"
    notebooks = runtime / "observe-decide-review" / "notebooks"
    notebooks.mkdir(parents=True)
    (notebooks / "redline.tsv").write_text("00004\tH5\n")
    (notebooks / "session.json").write_text('{"session_id":"game-123"}\n')
    before_notebooks = coach.capture_notebooks(runtime)
    monkeypatch.setattr(coach, "_runtime_skills_dir", lambda _player: runtime)
    install_calls = []

    def fake_install(_path):
        install_calls.append(_path)
        (notebooks / "redline.tsv").write_text("00000\tH0\n")
        (notebooks / "session.json").write_text('{"session_id":null}\n')
        (runtime / "observe-decide-review" / "SKILL.md").write_text("updated policy")
        if fail_install and len(install_calls) == 1:
            raise OSError("installation interrupted")
        return InstallRecord("", "new", str(runtime), "", {"mode": run_mode})

    monkeypatch.setattr(coach, "install_submission", fake_install)
    config_calls = []
    async def fake_config(*_args, **_kwargs):
        config_calls.append(1)
        return len(config_calls) > 1
    monkeypatch.setattr(coach, "reload_agent_config", fake_config)
    monkeypatch.setattr(coach, "_coach_output_dir", lambda: tmp_path / "coach-output")
    monkeypatch.setattr(coach, "_resolve_mode", lambda: "agent")
    monkeypatch.setattr(coach, "store_last_drive_session_id", lambda _session: None)
    pause_calls = []

    async def fake_pause(ws_url, session_id, *, mode, timeout_s):
        pause_calls.append((ws_url, session_id, mode, timeout_s))
        return {"success": True}

    monkeypatch.setattr(coach, "pause_agent_session", fake_pause)

    player_session_id = "old-player"
    expected_agent_session_id = "replacement-drive" if replace_player else "old-drive"
    coach._register_coach_session(
        db_path, player_session_id, "game-123", "old-drive", "game-123:7", context_bootstrap=True
    )
    if reuse_rewound:
        registry = coach._load_registry(db_path)
        registry[player_session_id]["jiuwen_rewound"] = "1"
        if run_mode == "team":
            registry[player_session_id]["jiuwen_team_reset"] = "1"
        registry[player_session_id]["context_source_game_session_id"] = "game-123"
        coach._save_registry(db_path, registry)
    old_gate = coach.Gate(coach.gate_path(db_path, "old-drive"))
    old_gate.create(
        "game-123", agent_session_id="old-drive", ws_url="ws://coach", mode=run_mode,
        existing_game=True, log_dir=str(tmp_path / "coach-output"), prompt="old",
    )
    old_gate.update(worker_status="stopped")
    args = Namespace(
        db=str(db_path),
        session_id=player_session_id,
        solution=str(solution_dir),
        agent_session_id=expected_agent_session_id if replace_player else "",
        ws_url="",
        reload_timeout_s=1.0,
    )
    if fail_install:
        with pytest.raises(OSError, match="installation interrupted"):
            await coach._reload_solution(args)
    with pytest.raises(RuntimeError, match="rejected agent.reload_config"):
        await coach._reload_solution(args)
    with pytest.raises(RuntimeError, match="Recovery has not been verified"):
        coach.Gate(coach.gate_path(db_path, expected_agent_session_id)).grant()
    output = await coach._reload_solution(args)
    assert coach.capture_notebooks(runtime) == before_notebooks
    assert (runtime / "observe-decide-review" / "SKILL.md").read_text() == "updated policy"
    assert output["ok"] is True
    assert output["session_id"] == expected_agent_session_id
    assert output["jiuwen_player_session_id"] == expected_agent_session_id
    assert output["game_session_id"] == "game-123"
    assert output["agent_session_id"] == expected_agent_session_id
    assert output["reused_agent_session"] is (not replace_player)
    assert output["reused_rewound_jiuwen_session"] is reuse_rewound
    assert output["context_history_rounds"] == 0
    assert "重新加载前的历史上下文" in Path(output["context_history_file"]).read_text()
    restored = await coach.read_session_payload(db_path, "game-123")
    assert restored["career"]["current_month"] == 7
    assert coach._load_registry(db_path)[expected_agent_session_id]["game_session_id"] == "game-123"
    assert coach._load_registry(db_path)[expected_agent_session_id]["context_bootstrap"] == "1"
    assert "jiuwen_rewound" not in coach._load_registry(db_path)[expected_agent_session_id]
    pause_count = 0 if reuse_rewound or run_mode == "agent" else 1 + int(fail_install) + int(replace_player)
    assert pause_calls == [("ws://coach", "old-drive", "team", 1.0)] * pause_count
    prompt = coach.Gate(coach.gate_path(db_path, expected_agent_session_id)).read()["prompt"]
    if reuse_rewound:
        assert "从 observe 开始完整一轮" in prompt
        assert "按恢复的笔记继续事件编号" in prompt
        assert "不复用撤回事件的上下文" in prompt
        assert "当前游戏状态是唯一事实" in prompt
        assert ("旧团队运行时及其任务记忆已清空" in prompt) is (run_mode == "team")
    elif replace_player:
        assert "只读取本角色文档" in prompt
        assert "执行其中列出的脚本" in prompt
        assert "不读取源码、其他角色文档或 coach/runner 文件" in prompt
        assert "SKILL.md、stages、scripts" not in prompt
    elif run_mode == "team":
        assert "同一 Player 会话" in prompt
        assert "现有团队状态" in prompt
    else:
        assert prompt == coach._step_prompt("game-123")
    assert "spawn_teammate" not in prompt
    if reuse_rewound or (run_mode == "team" and not replace_player):
        assert "不得创建或切换 agent_session_id" in prompt
    assert "恢复前历史上下文" not in prompt
    assert "previous-context-" not in prompt
    assert not (tmp_path / "debugs").exists()
    # A later successful run must not reuse the prior recovery's old notebooks.
    (notebooks / "redline.tsv").write_text("00005\tH7\n")
    (notebooks / "new-learning.tsv").write_text("new evidence\n")
    latest = coach.capture_notebooks(runtime)
    assert coach.Gate(coach.gate_path(db_path, expected_agent_session_id)).read()["reload_notebooks"] is None
    args.session_id = expected_agent_session_id
    again = await coach._reload_solution(args)
    assert again["ok"] is True
    assert coach.capture_notebooks(runtime) == latest


def test_bind_installed_game_session_only_updates_declared_notebooks(tmp_path: Path) -> None:
    declared = tmp_path / "one" / "notebooks" / "session.json"
    declared.parent.mkdir(parents=True)
    declared.write_text('{"session_id":null,"keep":1}\n', encoding="utf-8")
    unrelated = tmp_path / "two" / "notebooks" / "session.json"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text('{"keep":2}\n', encoding="utf-8")

    rebound = coach._bind_installed_game_session(tmp_path, "game-123")

    assert rebound == [declared]
    assert json.loads(declared.read_text(encoding="utf-8")) == {
        "session_id": "game-123",
        "keep": 1,
    }
    assert unrelated.read_text(encoding="utf-8") == '{"keep":2}\n'


@pytest.mark.asyncio
async def test_reload_rejects_rotating_a_rewound_agent_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rewind is recovered by its surviving Leader, never a replacement session."""
    from career_emulator.server.models import GameSession
    from career_emulator.storage import SessionStore

    db_path = tmp_path / "game.sqlite3"
    await SessionStore(db_path, tmp_path / "logs").save_session(GameSession(session_id="game-123"))
    coach._register_coach_session(
        db_path, "old-player", "game-123", "same-agent", "game-123:1", context_bootstrap=True
    )
    registry = coach._load_registry(db_path)
    registry["old-player"]["jiuwen_rewound"] = "1"
    coach._save_registry(db_path, registry)
    monkeypatch.setattr(coach, "ensure_runtime_dirs", lambda: None)

    with pytest.raises(RuntimeError, match="original agent_session_id"):
        await coach._reload_solution_impl(
            Namespace(
                db=str(db_path),
                session_id="old-player",
                solution="",
                agent_session_id="replacement-agent",
                ws_url="ws://coach",
                reload_timeout_s=1.0,
            )
        )


@pytest.mark.asyncio
async def test_replace_session_restores_payload_and_logs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A withdrawal restore must update state and the structured log table."""
    from career_emulator.server.models import GameSession, LogEntry
    from career_emulator.storage import SessionStore

    db_path = tmp_path / "game.sqlite3"
    log_dir = tmp_path / "logs"
    monkeypatch.setattr(coach, "default_emulator_log_dir", lambda: log_dir)
    store = SessionStore(db_path, log_dir)
    session = GameSession(session_id="game-123")
    await store.save_session(session)
    await store.append_log(LogEntry("game-123", "after", "action"))

    before = session.to_dict()
    before["career"]["current_month"] = 1
    result = await coach._replace_session(
        db_path,
        "game-123",
        before,
        [{"session_id": "game-123", "entry_type": "system", "message": "before"}],
    )
    assert result == {
        "session_id": "game-123",
        "rewound": True,
        "same_session_id": True,
        "session_exists": True,
    }
    restored = await coach.read_session_payload(db_path, "game-123")
    assert restored["career"]["current_month"] == 1
    assert (await store.load_logs("game-123"))[0].message == "before"


@pytest.mark.asyncio
async def test_emulator_rewind_does_not_modify_coach_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The controller rewind API owns game state only, never benchmark output."""
    from career_emulator.server.models import GameSession
    from career_emulator.storage import SessionStore

    db_path = tmp_path / "game.sqlite3"
    log_dir = tmp_path / "logs"
    report_dir = tmp_path / "coach-output"
    report_dir.mkdir()
    reports = {
        report_dir / "benchmark.md": b"benchmark contract\n",
        report_dir / "events.jsonl": b'{"event":"unchanged"}\n',
        tmp_path / "comparison.csv": b"solution_run,total_tokens_M\nrun,1.0\n",
    }
    for path, content in reports.items():
        path.write_bytes(content)

    session = GameSession(session_id="game-123")
    session.career.current_month = 2
    await SessionStore(db_path, log_dir).save_session(session)
    before = session.to_dict()
    before["career"]["current_month"] = 1
    monkeypatch.setattr(coach, "default_emulator_log_dir", lambda: log_dir)

    result = await coach._replace_session(db_path, "game-123", before, [])

    assert result["same_session_id"] is True
    assert (await coach.read_session_payload(db_path, "game-123"))["career"]["current_month"] == 1
    assert {path: path.read_bytes() for path in reports} == reports


def _exact_history_fixture(monkeypatch, tmp_path):
    """Give controller tests one explicit observe call; wire behavior is tested separately."""
    monkeypatch.setattr(coach, "read_history", lambda *_: [{"event_type": "chat.tool_call",
                         "tool_call": {"name": "observe", "tool_call_id": "observe-1"}}])
    original_append = coach.CheckpointStore.append
    def append(store, checkpoint):
        checkpoint.observe_boundary = {"tool_call_id": "observe-1"}
        original_append(store, checkpoint)
    monkeypatch.setattr(coach.CheckpointStore, "append", append)
    monkeypatch.setattr(coach, "_resolve_mode", lambda: "agent")
    monkeypatch.setattr(coach, "cancel_agent_session", lambda *_a, **_k: _async_value({"success": True}))


def _rewind_success(**extra):
    return {"rewind_context": True, "context_persisted": True, "history_persisted": True,
            "team_reset": True, **extra}


@pytest.mark.asyncio
@pytest.mark.parametrize("defer_reload", [False, True])
@pytest.mark.parametrize("replayed_hash_only", [False, True])
@pytest.mark.parametrize("legacy_context_mode", ["persistent", "isolated-events"])
async def test_withdraw_restores_latest_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, defer_reload, replayed_hash_only, legacy_context_mode
) -> None:
    """A successful withdrawal rewinds one state and leaves no checkpoint."""
    from career_emulator.server.models import GameSession
    from career_emulator.storage import SessionStore
    _exact_history_fixture(monkeypatch, tmp_path)

    db_path = tmp_path / "game.sqlite3"
    store = SessionStore(db_path, tmp_path / "logs")
    before = GameSession(session_id="game-123").to_dict()
    after = GameSession(session_id="game-123")
    after.career.current_month = 2
    await store.save_session(after)
    monkeypatch.setattr(coach, "default_emulator_log_dir", lambda: tmp_path / "logs")
    monkeypatch.setattr(coach, "_history_path", lambda _db, _sid: tmp_path / "history.jsonl")
    monkeypatch.setattr(coach, "resolve_instance_ws_url", lambda: "ws://coach")
    monkeypatch.setattr(coach, "_current_jiuwen_turn", _async_one)
    lifecycle_calls = []
    gate = coach.Gate(coach.gate_path(db_path, "drive"))
    gate.create(
        "game-123",
        mode="team",
        agent_session_id="drive",
    )
    gate.update(worker_status="running")

    async def fake_cancel(_url, session_id, **_kwargs):
        lifecycle_calls.append(("cancel", session_id))
        return {"success": True}

    async def fake_retire(control):
        lifecycle_calls.append(("retire", control.read()["agent_session_id"]))
        control.update(worker_status="stopped", revoked=True)

    monkeypatch.setattr(coach, "cancel_agent_session", fake_cancel)
    monkeypatch.setattr(coach, "retire_gated", fake_retire)

    async def fake_rewind(_url, session_id, **kwargs):
        lifecycle_calls.append(("rewind", session_id, kwargs["before_tool_call_id"]))
        return _rewind_success(session_id=session_id)
    monkeypatch.setattr(coach, "rewind_agent_session", fake_rewind)
    reload_calls = []
    async def fake_reload(_args):
        reload_calls.append(_args)
        return {"reloaded": True, "jiuwen_player_session_id": "game-123", "session_id": "game-123", "agent_session_id": "drive"}
    monkeypatch.setattr(coach, "_reload_solution", fake_reload)
    runtime = tmp_path / "runtime"
    notebooks = runtime / "observe-decide-review" / "notebooks"
    notebooks.mkdir(parents=True)
    (notebooks / "redline.tsv").write_text("00004\tH5\n")
    (notebooks / "dictionary.tsv").write_text("prior learning\n")
    before_notebooks = coach.capture_notebooks(runtime)
    (notebooks / "redline.tsv").write_text("00005\tH4\n")
    (notebooks / "dictionary.tsv").write_text("withdrawn learning\n")
    (notebooks / "future.md").write_text("withdrawn record\n")
    # A failed earlier reload must not override the selected withdrawal point.
    gate.update(reload_notebooks=coach.capture_notebooks(runtime))
    monkeypatch.setattr(coach, "_runtime_skills_dir", lambda _player: runtime)
    coach.CheckpointStore(db_path, "game-123").append(
        coach.Checkpoint(
            "game-123",
            "now",
            before,
            [],
            "stale-after-hash" if replayed_hash_only else coach._json_hash(after.to_dict()),
            "drive",
            decision={"event": {"title": "测试事件"}, "choice": {"number": 2, "action": "选项乙"}},
            state_transition={"before": {"health": 5}, "after": {"health": 4}, "changed": {"health": {}}},
            payload_delta=coach._payload_delta(before, after.to_dict()),
            before_notebooks=before_notebooks,
        )
    )

    from career_sim_runner.coach import benchmark
    output_dir = tmp_path / "benchmark"
    coach._save_state(db_path, benchmark_output_dir=str(output_dir),
                      context_mode=legacy_context_mode)
    coach._register_coach_session(db_path, "game-123", "game-123", "drive", "game-123:2")
    benchmark.begin(output_dir, round=1, game_session_id="game-123")
    output = await coach._withdraw(Namespace(db=str(db_path), session_id="game-123", defer_reload=defer_reload,
                                            reason="Wrong option", correction_kind="policy"))
    assert len(reload_calls) == (0 if defer_reload else 1)
    assert coach.capture_notebooks(runtime) == before_notebooks
    assert gate.read()["reload_notebooks"] == before_notebooks
    assert output["reload_after_withdraw"] is (not defer_reload)
    report = benchmark.read_ledger(output_dir)
    assert report["summary"]["coach_corrections"] == 1
    assert report["attempts"][0]["withdrawn_at"]
    assert output["withdrawn"] is True
    assert output["jiuwen_player_session_id"] == "game-123"
    assert lifecycle_calls == [
        ("cancel", "drive"),
        ("retire", "drive"),
        ("rewind", "drive", "observe-1"),
    ]
    assert output["observe_boundary"]["tool_call_id"] == "observe-1"
    assert coach._load_registry(db_path)["game-123"]["jiuwen_rewound"] == "1"
    assert coach._load_registry(db_path)["game-123"]["jiuwen_team_reset"] == "1"
    assert coach._load_registry(db_path)["game-123"]["context_source_game_session_id"] == "game-123"
    assert output["seed"] == "game-123:1"
    assert output["withdrawn_question"]["title"] == "测试事件"
    assert output["withdrawn_option"]["number"] == 2
    if not defer_reload:
        assert output["jiuwen_player_session_id"] == "game-123"
    assert output["withdrawn_record"]["decision"]["choice"]["number"] == 2
    assert "changed" in output["state_transition"]
    restored = await coach.read_session_payload(db_path, "game-123")
    assert restored["career"]["current_month"] == 1
    assert coach.CheckpointStore(db_path, "game-123").pop() is None


@pytest.mark.asyncio
async def test_withdraw_restores_game_after_elimination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A checkpoint remains withdrawable after a fatal health decision."""
    from career_emulator.server.models import GameSession
    from career_emulator.storage import SessionStore
    _exact_history_fixture(monkeypatch, tmp_path)

    db_path = tmp_path / "game.sqlite3"
    log_dir = tmp_path / "logs"
    before = GameSession(session_id="game-123")
    before.career.health = 1
    after = GameSession(session_id="game-123")
    after.career.health = 0
    after.career.alive = False
    after.career.failed = True
    after.career.failure_reason = "health reached zero"
    after.ending_score = {"outcome": "eliminated", "completed": False}
    store = SessionStore(db_path, log_dir)
    await store.save_session(after)
    monkeypatch.setattr(coach, "default_emulator_log_dir", lambda: log_dir)
    monkeypatch.setattr(coach, "_history_path", lambda _db, _sid: tmp_path / "history.jsonl")
    monkeypatch.setattr(coach, "resolve_instance_ws_url", lambda: "ws://coach")
    monkeypatch.setattr(coach, "_current_jiuwen_turn", _async_one)
    monkeypatch.setattr(
        coach,
        "rewind_agent_session",
        lambda *_args, **_kwargs: _async_value(_rewind_success()),
    )
    async def fake_reload(_args):
        return {"reloaded": True, "jiuwen_player_session_id": "game-123", "session_id": "game-123", "agent_session_id": "drive"}
    monkeypatch.setattr(coach, "_reload_solution", fake_reload)
    coach.CheckpointStore(db_path, "game-123").append(
        coach.Checkpoint("game-123", "fatal-step", before.to_dict(), [], coach._json_hash(after.to_dict()), "drive")
    )

    coach._register_coach_session(db_path, "game-123", "game-123", "drive", "")
    output = await coach._withdraw(Namespace(db=str(db_path), session_id="game-123"))

    assert output["withdrawn"] is True
    restored = await coach.read_session_payload(db_path, "game-123")
    assert restored["career"]["health"] == 1
    assert restored["career"]["alive"] is True
    assert restored["career"]["failed"] is False
    assert restored["ending_score"] is None


@pytest.mark.asyncio
async def test_step_checkpoints_eliminating_action_before_withdraw(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real step path records a fatal action before terminal observation."""
    from career_emulator.server.models import GameSession
    from career_emulator.storage import SessionStore
    from career_sim_runner.models import TokenUsage
    from career_sim_runner.ws_client import StepDriveResult
    _exact_history_fixture(monkeypatch, tmp_path)

    db_path = tmp_path / "game.sqlite3"
    log_dir = tmp_path / "logs"
    output_dir = tmp_path / "coach-output"
    before = GameSession(session_id="game-123")
    before.career.health = 1
    store = SessionStore(db_path, log_dir)
    await store.save_session(before)
    monkeypatch.setattr(coach, "default_emulator_log_dir", lambda: log_dir)
    monkeypatch.setattr(coach, "ensure_runtime_dirs", lambda: None)
    monkeypatch.setattr(coach, "load_active_install", lambda: None)
    monkeypatch.setattr(coach, "_resolve_agent_session", lambda *_args, **_kwargs: "drive")
    monkeypatch.setattr(coach, "store_last_drive_session_id", lambda _session: None)
    monkeypatch.setattr(coach, "_coach_output_dir", lambda: output_dir)
    monkeypatch.setattr(
        coach,
        "_ensure_coach_emulator_log_dir",
        lambda *_args, **_kwargs: (log_dir, False),
    )
    async def fake_reload(_args):
        return {"reloaded": True, "jiuwen_player_session_id": "game-123", "session_id": "game-123", "agent_session_id": "drive"}
    monkeypatch.setattr(coach, "_reload_solution", fake_reload)

    coach._register_coach_session(db_path, "game-123", "game-123", "drive", "game-123:1")
    gate = coach.Gate(coach.gate_path(db_path, "drive"))
    gate.create("game-123", log_dir=str(output_dir))

    async def fatal_step(control, _timeout):
        control.update(before_notebooks={})
        after = GameSession(session_id="game-123")
        after.career.health = 0
        after.career.alive = False
        after.career.failed = True
        after.career.failure_reason = "health reached zero"
        await store.save_session(after)
        control.completed("game-123", {"success": True}, 1, "fatal")
        return StepDriveResult(
            exit_code=0,
            termination_reason=None,
            session_id="game-123",
            action_success=True,
            action_result={"success": True},
            token_usage=TokenUsage(),
            transcript="",
            events_path=output_dir / "events.jsonl",
            transcript_path=output_dir / "transcript.log",
        )

    monkeypatch.setattr(coach, "drive_gated", fatal_step)
    monkeypatch.setattr(coach, "_current_jiuwen_turn", _async_one)
    copied_notebooks = output_dir / "notebooks" / "skill-a" / "state-keeps.md"
    copy_calls = []
    monkeypatch.setattr(
        coach,
        "_copy_keeps_notebooks",
        lambda agent_session_id, destination: copy_calls.append((agent_session_id, destination))
        or [copied_notebooks],
    )
    monkeypatch.setattr(
        coach,
        "rewind_agent_session",
        lambda *_args, **_kwargs: _async_value(_rewind_success()),
    )
    args = Namespace(
        db=str(db_path),
        session_id="game-123",
        agent_session_id="",
        ws_url="ws://coach",
        mode="agent",
        timeout_s=1.0,
    )

    output = await coach._run_step(args)
    assert output["ok"] is True
    assert copy_calls == [("drive", output_dir)]
    assert output["debug_notebooks"] == [str(copied_notebooks)]
    restored = await coach._withdraw(args)
    assert restored["withdrawn"] is True
    payload = await coach.read_session_payload(db_path, "game-123")
    assert payload["career"]["health"] == 1
    assert payload["career"]["alive"] is True


def test_rewrite_session_ids_covers_nested_context() -> None:
    context = {
        "game_session_id": "old-game",
        "question": ["session_id=old-game", {"old-game": "tool(old-game)"}],
        "count": 3,
    }
    assert coach._rewrite_session_ids(context, "old-game", "new-game") == {
        "game_session_id": "new-game",
        "question": ["session_id=new-game", {"new-game": "tool(new-game)"}],
        "count": 3,
    }


@pytest.mark.asyncio
async def test_withdraw_rewind_failure_does_not_change_game(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Jiuwen must rewind before the emulator checkpoint is mutated."""
    from career_emulator.server.models import GameSession
    from career_emulator.storage import SessionStore
    _exact_history_fixture(monkeypatch, tmp_path)

    db_path = tmp_path / "game.sqlite3"
    log_dir = tmp_path / "logs"
    before = GameSession(session_id="game-123")
    after = GameSession(session_id="game-123")
    after.career.current_month = 2
    await SessionStore(db_path, log_dir).save_session(after)
    monkeypatch.setattr(coach, "default_emulator_log_dir", lambda: log_dir)
    monkeypatch.setattr(coach, "_history_path", lambda _db, _sid: tmp_path / "history.json")
    monkeypatch.setattr(coach, "resolve_instance_ws_url", lambda: "ws://coach")
    coach.CheckpointStore(db_path, "game-123").append(
        coach.Checkpoint(
            "game-123", "now", before.to_dict(), [], coach._json_hash(after.to_dict()),
            "drive", jiuwen_turn_index=1,
        )
    )

    async def fail_rewind(*_args, **_kwargs):
        raise RuntimeError("rewind failed")
    monkeypatch.setattr(coach, "rewind_agent_session", fail_rewind)

    with pytest.raises(RuntimeError, match="rewind failed"):
        await coach._withdraw(Namespace(db=str(db_path), session_id="game-123"))
    assert (await coach.read_session_payload(db_path, "game-123"))["career"]["current_month"] == 2
    assert len(coach.CheckpointStore(db_path, "game-123").records()) == 1


@pytest.mark.asyncio
async def test_step_does_not_rotate_or_retry_after_empty_turn(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An empty turn preserves the agent id and reports the live state without retries."""
    from career_emulator.server.models import GameSession
    from career_emulator.storage import SessionStore
    from career_sim_runner.models import TokenUsage
    from career_sim_runner.ws_client import StepDriveResult

    db_path = tmp_path / "game.sqlite3"
    log_dir = tmp_path / "logs"
    output_dir = tmp_path / "coach-output"
    store = SessionStore(db_path, log_dir)
    await store.save_session(GameSession(session_id="game-123"))
    coach._register_coach_session(db_path, "coach-1", "game-123", "old-agent", "game-123:1")
    gate = coach.Gate(coach.gate_path(db_path, "old-agent"))
    gate.create("game-123", log_dir=str(output_dir))

    monkeypatch.setattr(coach, "default_emulator_log_dir", lambda: log_dir)
    monkeypatch.setattr(coach, "ensure_runtime_dirs", lambda: None)
    monkeypatch.setattr(coach, "load_active_install", lambda: None)
    monkeypatch.setattr(coach, "_coach_output_dir", lambda: output_dir)
    monkeypatch.setattr(
        coach,
        "_ensure_coach_emulator_log_dir",
        lambda *_args, **_kwargs: (log_dir, False),
    )
    monkeypatch.setattr(coach, "store_last_drive_session_id", lambda _session: None)
    ids = iter(("fresh-1", "fresh-2"))
    monkeypatch.setattr(coach, "new_drive_session_id", lambda: next(ids))

    calls: list[str] = []

    async def empty_then_success(control, _timeout):
        calls.append(control.path.name)
        if len(calls) == 1:
            return StepDriveResult(
                exit_code=1,
                termination_reason="agent_stopped_before_action",
                session_id=None,
                action_success=None,
                action_result=None,
                token_usage=TokenUsage(),
                transcript="",
                events_path=output_dir / "empty-events.jsonl",
                transcript_path=output_dir / "empty-transcript.log",
            )
        return StepDriveResult(
            exit_code=0,
            termination_reason=None,
            session_id="game-123",
            action_success=True,
            action_result={"success": True},
            token_usage=TokenUsage(),
            transcript="",
            events_path=output_dir / "success-events.jsonl",
            transcript_path=output_dir / "success-transcript.log",
        )

    monkeypatch.setattr(coach, "drive_gated", empty_then_success)
    args = Namespace(
        db=str(db_path),
        session_id="coach-1",
        agent_session_id="",
        ws_url="ws://coach",
        mode="agent",
        timeout_s=1.0,
    )

    output = await coach._run_step(args)

    assert output["ok"] is False
    assert output["session_id"] == "coach-1"
    assert output["jiuwen_player_session_id"] == "coach-1"
    assert output["agent_session_id"] == "old-agent"
    assert calls == [gate.path.name]
    assert output["inspection"]["current_question"] is not None
    assert coach._load_registry(db_path)["coach-1"]["agent_session_id"] == "old-agent"


@pytest.mark.asyncio
async def test_peek_does_not_consume_feedback_or_change_payload(tmp_path, monkeypatch):
    from career_emulator.server.models import GameSession
    from career_emulator.storage import SessionStore

    db = tmp_path / "game.db"
    store = SessionStore(db, tmp_path / "logs")
    session = GameSession(session_id="game")
    session.pending_observe_warning = "本月工资到账；HR提醒"
    await store.save_session(session)
    monkeypatch.setattr(coach, "_coach_emulator_log_dir", lambda _: tmp_path / "logs")
    before = await coach.read_session_payload(db, "game")
    first = await coach._peek(db, "game")
    second = await coach._peek(db, "game")
    assert first["observation"]["warning"] == second["observation"]["warning"] == session.pending_observe_warning
    assert await coach.read_session_payload(db, "game") == before
