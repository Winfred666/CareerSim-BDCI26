"""Tests for the unified emulator/coach evidence log."""

from pathlib import Path

from career_sim_runner.coach.log import (
    append_coach_step_log,
    read_coach_step_logs,
    read_session_log_tail,
    rewrite_session_log,
)


def _record(round_number: int) -> dict:
    return {
        "round": round_number,
        "question": {"event": {"title": f"事件 {round_number}"}, "choices": [{"choice": 1}]},
        "decision": {"choice": {"number": 1}},
        "action_result": {"success": True},
        "consequence": {"changed": {"network": {"before": 1, "after": 3}}},
        "state_after": {"status": {"network": 3, "skill": 2, "dignity": 6}},
        "next_question": {"event": {"title": f"下一事件 {round_number}"}, "choices": []},
        "created_at": "now",
    }


def test_append_and_tail_read_preserve_choices_and_full_state(tmp_path: Path) -> None:
    path = append_coach_step_log(
        tmp_path,
        "game-1",
        round_number=1,
        question=_record(1)["question"],
        decision=_record(1)["decision"],
        action_result={"success": True},
        consequence=_record(1)["consequence"],
        state_after=_record(1)["state_after"],
        next_question=_record(1)["next_question"],
    )
    records = read_coach_step_logs(path)
    assert records[0]["question"]["choices"] == [{"choice": 1}]
    assert records[0]["state_after"]["status"]["network"] == 3
    assert read_session_log_tail(path)["lines"]


def test_rewrite_retains_coach_records_after_withdrawal(tmp_path: Path) -> None:
    path = tmp_path / "game-1.log"
    rewrite_session_log(path, [{"entry_type": "action", "message": "restored"}], [_record(1)])
    assert read_coach_step_logs(path)[0]["round"] == 1
    assert "restored" in path.read_text(encoding="utf-8")


def test_career_log_preserves_withdrawn_state_and_retry_across_refresh(tmp_path: Path) -> None:
    from career_sim_runner.coach import benchmark

    attempt = benchmark.begin(tmp_path, game_session_id="game-1", round=1)
    benchmark.finish(tmp_path, attempt, career_state={
        "question": _record(1)["question"],
        "choice": {"number": 1, "action": "休息"},
        "transition": {"changed": {"health": {"before": 5, "after": 6}}},
        "state_after": {"health": 6, "alive": True, "ending_score": {"quantitative_score": 42}},
        "next_question": _record(1)["next_question"],
    })
    path = tmp_path / "career.log"
    first = path.read_text(encoding="utf-8")
    assert "已提交" in first and "休息" in first
    assert "before: 5" in first and "after: 6" in first
    assert "quantitative_score: 42" in first
    benchmark.mark_withdrawn(tmp_path, "game-1", 1)
    retry = benchmark.begin(tmp_path, game_session_id="game-1", round=1)
    benchmark.finish(tmp_path, retry, termination_reason="timeout", action_success=False)
    benchmark.refresh(tmp_path, tmp_path / "missing.jsonl")
    final = path.read_text(encoding="utf-8")
    assert "尝试 1 | 回合 1 | 已撤回" in final
    assert "尝试 2 | 回合 1 | 未记录状态提交" in final
    assert "timeout" in final and "health: 6" in final
    assert final.count("quantitative_score: 42") == 1
