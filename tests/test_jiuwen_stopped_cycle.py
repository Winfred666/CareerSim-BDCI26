"""Finished native cycles must not prevent old team pollers from stopping."""
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from openjiuwen.agent_teams.agent.coordination.event_bus import EventBus
from openjiuwen.agent_teams.agent.coordination.kernel import CoordinationKernel
from openjiuwen.agent_teams.harness.state import HarnessState
from openjiuwen.agent_teams.harness.team_harness import TeamHarness
from openjiuwen.agent_teams.schema.team import TeamRole


@pytest.mark.asyncio
async def test_terminated_native_with_pending_session_commit_does_not_leak_pollers():
    harness = object.__new__(TeamHarness)
    native = SimpleNamespace(state=HarnessState.TERMINATED,
                             abort=AsyncMock(side_effect=RuntimeError('NativeHarness already stopped.')))
    harness._native = native
    harness._active_agent_session = object()  # stop() has not finished committing.
    harness.dispose = AsyncMock()
    host = SimpleNamespace(
        member_name='probe', role=TeamRole.TEAMMATE,
        stream_controller=SimpleNamespace(drain_agent_task=harness.abort, close_stream=Mock()),
        persist_allocator_state=Mock(),
        resources=SimpleNamespace(memory_manager=None, harness=harness),
        spawn_manager=SimpleNamespace(cancel_recovery_tasks=AsyncMock(), shutdown_all_handles=AsyncMock()),
        infra=SimpleNamespace(messager=None),
        session_manager=SimpleNamespace(release_session=Mock()),
    )
    kernel = CoordinationKernel(host)
    kernel._lifecycle_state = 'running'
    bus = EventBus(role=TeamRole.TEAMMATE)
    kernel._event_bus = bus
    await bus.start()
    tasks = [bus._loop_task, bus._mailbox_poll_task, bus._task_poll_task]
    try:
        await kernel.stop()
        native.abort.assert_not_awaited()
        assert not bus.is_running
        assert all(task.done() for task in tasks)
        host.session_manager.release_session.assert_called_once()
        harness.dispose.assert_awaited_once()
    finally:
        await bus.stop()


@pytest.mark.asyncio
async def test_live_native_still_receives_abort():
    harness = object.__new__(TeamHarness)
    harness._native = SimpleNamespace(state=HarnessState.RUNNING, abort=AsyncMock())
    harness._active_agent_session = object()
    await harness.abort(immediate=True)
    harness._native.abort.assert_awaited_once_with(immediate=True)
