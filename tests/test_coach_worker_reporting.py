import asyncio
import json
import threading

import pytest

from career_sim_runner.coach import worker
from career_sim_runner.coach.gate import Gate


@pytest.mark.asyncio
async def test_slow_reports_do_not_block_socket_reads(tmp_path, monkeypatch):
    gate = Gate(tmp_path / "gate.db")
    gate.create("game", log_dir=str(tmp_path / "logs"), ws_url="ws://test",
                mode="agent", agent_session_id="same-agent", prompt="continue", existing_game=True)
    frames = asyncio.Queue()
    reporting = threading.Event()
    release = threading.Event()
    received = asyncio.Event()
    calls = 0
    request_id = None

    def slow_refresh(*args, **kwargs):
        reporting.set()
        assert release.wait(3)

    class Socket:
        async def __aenter__(self):
            await frames.put("{}")
            return self

        async def __aexit__(self, *args):
            return False

        async def recv(self):
            nonlocal calls
            frame = await frames.get()
            calls += 1
            if calls >= 3:
                received.set()
            return frame

        async def send(self, body):
            nonlocal request_id
            request_id = json.loads(body)['request_id']

    monkeypatch.setattr(worker.websockets, "connect", lambda *a, **k: Socket())
    monkeypatch.setattr(worker, "refresh", slow_refresh)
    monkeypatch.setattr(worker, "render_events", lambda *args: None)
    task = asyncio.create_task(worker.run(gate))
    try:
        while gate.read()["worker_status"] != "running":
            await asyncio.sleep(0.01)
        await frames.put(json.dumps({"request_id": request_id, "event_type": "chat.tool_call", "tool_call": {"name": "observe"}}))
        assert await asyncio.to_thread(reporting.wait, 1)
        await frames.put("{}")
        await asyncio.wait_for(received.wait(), 1)
    finally:
        release.set()
        gate.revoke()
        await asyncio.wait_for(task, 2)
