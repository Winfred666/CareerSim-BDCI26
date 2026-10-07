"""Auto-postmortem keeps playing across every missed review without deadlines."""
import pytest

from career_sim_runner.coach import cli as coach


@pytest.mark.asyncio
@pytest.mark.parametrize("resume", [False, True])
async def test_missed_reviews_never_stop_an_unassisted_run(monkeypatch, tmp_path, resume):
    months = iter([6, 12, 18, 24, 30, 36, 42, 48])
    current = 6
    calls = []

    async def step(args, *, start=False):
        nonlocal current
        assert args.timeout_s == float("inf")
        calls.append((start, args.session_id, args.seed))
        current = next(months)
        return {"ok": True, "game_session_id": "g", "jiuwen_player_session_id": "p",
                "seed": f"seed:{current}"}

    async def peek(*a):
        ending = {"outcome": "completed", "completed": True, "survival_months": 48,
                  "quantitative_score": 10} if current == 48 else None
        return {"observation": {"current_state": {
            "status": {"level": "L1"}, "statistics": {"last_talk_month": current},
            "simulation_flags": {"alive": True, "failed": False}}, "ending_score": ending}}

    async def resolve(*a):
        return {"game_session_id": "g", "agent_session_id": "p", "seed": "seed:6"}

    monkeypatch.setattr(coach, "_run_step", step)
    monkeypatch.setattr(coach, "_peek", peek)
    monkeypatch.setattr(coach, "_resolve_context", resolve)
    args = coach._parser().parse_args(["auto-postmortem"])
    args.db = str(tmp_path / "game.sqlite3")
    args.session_id = "p" if resume else ""
    result = await coach._run_auto_postmortem(args)
    assert len(calls) == 8
    assert calls[0][0] is (not resume)
    assert calls[1][1:] == ("p", "seed:6")
    assert result["terminal"] and result["terminal_outcome"] == "completed"
    assert result["events_completed"] == 8
    assert result["ending_score"]["quantitative_score"] == 10


@pytest.mark.parametrize("flag", ["--through-review-month", "--allow-missed-review-month",
                                  "--allow-missed-reviews-from-month", "--max-runtime-s",
                                  "--max-events", "--timeout-s"])
def test_auto_postmortem_has_no_coach_stop_flags(flag):
    with pytest.raises(SystemExit):
        coach._parser().parse_args(["auto-postmortem", flag, "6"])
