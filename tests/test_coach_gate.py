"""Execution-boundary tests; no live Jiuwen instance or competition DB."""

import asyncio
import json

import pytest
from fastmcp import Client

from career_sim_runner.coach.gate import Gate
from career_sim_runner.emulator_adapter import EmulatorAdapter, make_server


class Response:
    def __init__(self, value):
        self.value = value

    def to_dict(self):
        return self.value

    to_mcp_dict = to_dict


class Engine:
    async def _get_promotion_applicator(self):
        pass

    def __init__(self):
        self.actions = []
        self.observations = 0
        self.logs = 0

    async def new_game(self):
        return Response({"session_id": "game"})

    async def observe(self, session_id):
        self.observations += 1
        return Response({"current_state": {"session_id": session_id, "status": {"health": 5}},
                         "current_event": {"title": "季度行动"}, "choices": [{"choice": 7}],
                         "events": "本月工资到账"})

    async def take_action(self, session_id, choice, notes):
        self.actions.append(choice)
        await asyncio.sleep(0)
        return Response({"success": True, "error": None})

    async def latest_logs(self, session_id, count):
        self.logs += 1
        return {"logs": []}

    async def enrollment_handbook(self):
        return "handbook"


@pytest.mark.asyncio
async def test_parallel_actions_cannot_cross_execution_gate(tmp_path):
    gate = Gate(tmp_path / "gate.db")
    gate.create("game")
    engine = Engine()
    # Different MCP processes share the SQLite boundary, not an asyncio lock.
    a = EmulatorAdapter(engine, tmp_path / "snapshots", gate)
    b = EmulatorAdapter(engine, tmp_path / "snapshots", Gate(gate.path))
    gate.grant()
    first = asyncio.create_task(a.take_action("game", 7, "first"))
    second = asyncio.create_task(b.take_action("game", 6, "second"))
    try:
        await first
        await asyncio.sleep(0.1)
        assert engine.actions == [7]
        assert not second.done()
        with pytest.raises(RuntimeError, match="checkpointed"):
            gate.grant()
        gate.acknowledge()
        gate.grant()
        assert (await asyncio.wait_for(second, 1))["success"]
        assert engine.actions == [7, 6]
    finally:
        second.cancel()
        await asyncio.gather(second, return_exceptions=True)


@pytest.mark.asyncio
async def test_pause_blocks_observe_but_allows_reviewer_logs(tmp_path):
    gate = Gate(tmp_path / "gate.db")
    gate.create("game")
    engine = Engine()
    adapter = EmulatorAdapter(engine, tmp_path / "snapshots", gate)
    pending = asyncio.create_task(adapter.observe("game"))
    try:
        async with Client(make_server(adapter)) as client:
            await client.call_tool("check_latest_logs", {"session_id": "game", "count": 3})
        assert engine.logs == 1
        assert engine.observations == 0
        gate.grant()
        response = await asyncio.wait_for(pending, 1)
        from pathlib import Path
        path = Path(response.pop("observe_json_path"))
        assert path.is_absolute()
        assert json.loads(path.read_text()) == response
        assert response["events"] == "本月工资到账"
        assert gate.read()["permit"]  # observe did not spend the action token
    finally:
        pending.cancel()
        await asyncio.gather(pending, return_exceptions=True)


@pytest.mark.asyncio
async def test_revoked_conversation_cannot_execute_and_wrong_game_is_rejected(tmp_path):
    gate = Gate(tmp_path / "gate.db")
    gate.create("game")
    engine = Engine()
    adapter = EmulatorAdapter(engine, tmp_path / "snapshots", gate)
    gate.grant()
    with pytest.raises(RuntimeError, match="belong"):
        await adapter.take_action("other", 1, "")
    with pytest.raises(RuntimeError, match="new game"):
        await adapter.new_game()
    gate.revoke()
    with pytest.raises(RuntimeError, match="retired"):
        await adapter.take_action("game", 1, "")
    assert not engine.actions


@pytest.mark.asyncio
async def test_mcp_schema_and_ordinary_play_without_gate(tmp_path):
    engine = Engine()
    adapter = EmulatorAdapter(engine, tmp_path / "snapshots")
    async with Client(make_server(adapter)) as client:
        tools = {t.name: t for t in await client.list_tools()}
        assert set(tools) == {"new_game", "observe", "take_action", "check_latest_logs", "show_employee_handbook"}
        assert set(tools["take_action"].inputSchema["properties"]) == {"session_id", "choice", "notes"}
        for choice in (7, 6):
            await client.call_tool("take_action", {"session_id": "game", "choice": choice, "notes": ""})
    assert engine.actions == [7, 6]


@pytest.mark.asyncio
async def test_worker_keeps_one_socket_and_one_prompt_across_steps(tmp_path, monkeypatch):
    from career_sim_runner.coach import driver as coach_driver
    from career_sim_runner.coach import worker as coach_worker

    gate = Gate(tmp_path / "gate.db")
    gate.create("game", log_dir=str(tmp_path / "logs"), ws_url="ws://test",
                mode="team", agent_session_id="same-agent", prompt="按solution继续", existing_game=True)
    adapter = EmulatorAdapter(Engine(), tmp_path / "snapshots", gate)
    frames = asyncio.Queue()
    sent = []

    class Socket:
        async def __aenter__(self):
            await frames.put("{}")
            return self

        async def __aexit__(self, *args):
            return False

        async def recv(self):
            return await frames.get()

        async def send(self, body):
            sent.append(json.loads(body))

    monkeypatch.setattr(coach_worker.websockets, "connect", lambda *a, **k: Socket())
    worker = asyncio.create_task(coach_worker.run(gate))
    while gate.read()["worker_status"] != "running":
        await asyncio.sleep(0.01)
    for choice in (7, 6):
        call = asyncio.create_task(adapter.take_action("game", choice, ""))
        result = await coach_driver.drive(gate, 1)
        await call
        assert result.action_success
        gate.acknowledge()
        assert not worker.done()
    gate.revoke()
    await asyncio.wait_for(worker, 2)
    assert len(sent) == 1
    assert sent[0]["session_id"] == "same-agent"


@pytest.mark.asyncio
async def test_timeout_closes_unused_permit_without_retry(tmp_path, monkeypatch):
    from career_sim_runner.coach import driver as coach_driver
    import os

    gate = Gate(tmp_path / "gate.db")
    gate.create("game", log_dir=str(tmp_path), existing_game=True)
    gate.update(worker_status="running", worker_pid=os.getpid())
    result = await coach_driver.drive(gate, 0.01)
    assert not result.action_success
    assert not gate.read()["permit"]
    assert gate.read()["sequence"] == 0


@pytest.mark.asyncio
async def test_real_stdio_adapter_and_emulator_pause_before_second_action(tmp_path):
    """Exercise the actual MCP child process and game engine, in an isolated DB."""
    import sys
    from pathlib import Path
    from fastmcp.client.transports import StdioTransport
    from career_sim_runner.db import read_session_payload

    gate = Gate(tmp_path / "gate.db")
    gate.create()
    gate.grant()
    database = tmp_path / "game.db"
    transport = StdioTransport(
        command=sys.executable, args=["-m", "career_sim_runner.emulator_adapter"],
        cwd=str(Path(__file__).resolve().parents[1]), keep_alive=False,
        env={"CAREER_EMULATOR_DB": str(database), "CAREER_EMULATOR_LOG_DIR": str(tmp_path / "logs"),
             "CAREER_OBSERVE_SNAPSHOTS": str(tmp_path / "snapshots"), "CAREER_COACH_GATE": str(gate.path)},
    )
    async with Client(transport) as client:
        created = await client.call_tool("new_game", {})
        session_id = created.data["session_id"]
        observed = (await client.call_tool("observe", {"session_id": session_id})).data
        snapshot = json.loads(Path(observed["observe_json_path"]).read_text())
        assert snapshot == {k: v for k, v in observed.items() if k != "observe_json_path"}
        choice = observed["choices"][0]["choice"]
        first = await client.call_tool("take_action", {"session_id": session_id, "choice": choice, "notes": "test"})
        assert first.data["success"]
        after = await read_session_payload(database, session_id)
        pending = asyncio.create_task(client.call_tool("observe", {"session_id": session_id}))
        try:
            await asyncio.sleep(0.2)
            assert not pending.done()
            assert await read_session_payload(database, session_id) == after
            gate.acknowledge()
            gate.grant()
            next_observed = (await asyncio.wait_for(pending, 5)).data
            choice = next_observed["choices"][0]["choice"]
            second = await client.call_tool("take_action", {"session_id": session_id, "choice": choice, "notes": "test2"})
            assert second.data["success"]
            assert gate.read()["sequence"] == 2
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
