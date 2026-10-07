"""Long conversations must not consume later model calls' retry budgets."""
import pytest

from career_sim_runner.jiuwen_patch import ensure_openjiuwen_control_patch

ensure_openjiuwen_control_patch()
# Load dependency classes only after applying the pinned compatibility patches.
from openjiuwen.core.single_agent.rail.base import AgentCallbackContext  # noqa: E402
from openjiuwen.harness.rails.model_anomaly_detection_rail import ModelAnomalyDetectionRail  # noqa: E402


@pytest.mark.asyncio
@pytest.mark.parametrize('message', [
    'LLM repeated stream output detected', 'LLM stream timeout',
])
async def test_successful_calls_replenish_retry_budget(message):
    retry = ModelAnomalyDetectionRail(max_retries=2, backoff_seconds=[0])
    ctx = AgentCallbackContext(agent=None)
    await retry.before_invoke(ctx)
    for _ in range(4):
        ctx.exception = RuntimeError(message)
        await retry.on_model_exception(ctx)
        assert ctx.consume_retry_request() is not None
        await retry.after_model_call(ctx)
        ctx.exception = None
        await retry.after_model_call(ctx)


@pytest.mark.asyncio
@pytest.mark.parametrize('message', [
    'LLM repeated stream output detected', 'LLM stream timeout',
])
async def test_consecutive_failures_still_stop_after_two_retries(message):
    retry = ModelAnomalyDetectionRail(max_retries=2, backoff_seconds=[0])
    ctx = AgentCallbackContext(agent=None, exception=RuntimeError(message))
    await retry.before_invoke(ctx)
    for expected in (True, True, False):
        await retry.on_model_exception(ctx)
        assert (ctx.consume_retry_request() is not None) is expected
        await retry.after_model_call(ctx)


@pytest.mark.asyncio
async def test_unrelated_model_error_is_not_retried():
    import httpx
    from openai import AuthenticationError
    retry = ModelAnomalyDetectionRail()
    request = httpx.Request('POST', 'https://model.invalid/chat/completions')
    error = AuthenticationError('authentication failed',
                                response=httpx.Response(401, request=request), body={})
    ctx = AgentCallbackContext(agent=None, exception=error)
    await retry.on_model_exception(ctx)
    assert ctx.consume_retry_request() is None


@pytest.mark.asyncio
async def test_task_failure_survives_result_stream_and_client_parser():
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, Mock
    from openjiuwen.harness.task_loop.task_loop_event_handler import TaskLoopEventHandler
    from openjiuwen.core.single_agent.agents.react_agent import ReActAgent
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter

    error = 'LLM repeated stream output detected'
    handler = TaskLoopEventHandler(deep_agent=None)
    handler._resolve_future = Mock()
    await handler.handle_task_failed(SimpleNamespace(
        event=SimpleNamespace(metadata={}, error_message=error)))
    result = handler._resolve_future.call_args.args[0]
    session = SimpleNamespace(write_stream=AsyncMock())
    await ReActAgent._write_invoke_result_to_stream(None, result, session)
    chunk = session.write_stream.call_args.args[0]
    parsed = JiuWenSwarmDeepAdapter._parse_stream_chunk(chunk)
    assert parsed['event_type'] == 'chat.error'
    assert error in str(parsed)
