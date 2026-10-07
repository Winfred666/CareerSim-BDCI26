"""A new MCP process must enforce the same caps as an uninterrupted game."""

import pytest

from career_emulator.game import GameEngine
from career_emulator.server.models import CareerState, GameSession, get_stat_clamp_requirements, set_stat_clamp_requirements
from career_emulator.storage import SessionStore
from career_sim_runner.emulator_adapter import EmulatorAdapter


@pytest.fixture
def cold_clamps():
    previous = get_stat_clamp_requirements()
    set_stat_clamp_requirements({})
    try:
        yield
    finally:
        set_stat_clamp_requirements(previous)


def engine_at(tmp_path):
    engine = GameEngine(db_path=tmp_path / "game.db")
    engine.store = SessionStore(tmp_path / "game.db", tmp_path / "logs")
    return engine


@pytest.mark.asyncio
@pytest.mark.parametrize("observe_first", [False, True])
async def test_restored_first_energy_action_cannot_exceed_l4_cap(tmp_path, cold_clamps, observe_first):
    engine = engine_at(tmp_path)
    session = GameSession(
        session_id="restored",
        career=CareerState(level="L4", current_month=27, skill=80, output=26, network=36,
                           health=8, dignity=8, wealth=20, energy=3),
        initialization_seen=True, pending_fixed_event="energy_action", quarter_energy_remaining=3,
    )
    await engine.store.save_session(session)
    adapter = EmulatorAdapter(engine, tmp_path / "observations")
    if observe_first:
        before = await adapter.observe("restored")
        assert before["current_state"]["status"]["output"] == 26
    assert (await adapter.take_action("restored", 1, ""))["success"]  # O+2
    after = await engine.store.load_session("restored")
    assert after.career.output == 26
    assert after.career.health == 7  # The real action still executes.
    assert after.career.current_month == 27  # No month boundary loaded the caps.
    assert (await adapter.take_action("restored", 3, ""))["success"]  # N+1
    after = await engine.store.load_session("restored")
    assert after.career.network == 36
    assert get_stat_clamp_requirements()["L4"] == {"skill": 108, "network": 36, "output": 26}


@pytest.mark.asyncio
async def test_new_game_initializes_caps_before_any_action(tmp_path, cold_clamps):
    adapter = EmulatorAdapter(engine_at(tmp_path), tmp_path / "observations")
    result = await adapter.new_game()
    assert result["session_id"]
    assert get_stat_clamp_requirements()["L1"] == {"skill": 9, "network": 3, "output": 3}


@pytest.mark.asyncio
async def test_rule_load_failure_prevents_action(tmp_path):
    class BrokenEngine:
        async def _get_promotion_applicator(self):
            raise ValueError("broken promotion config")

        async def take_action(self, *args):
            pytest.fail("Must not execute an uncapped action")

    with pytest.raises(ValueError, match="broken promotion config"):
        await EmulatorAdapter(BrokenEngine(), tmp_path).take_action("game", 1, "")
