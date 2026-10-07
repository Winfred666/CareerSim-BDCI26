import asyncio,json,runpy
from pathlib import Path
import pytest
SKILL=Path(__file__).resolve().parents[1]/'skills/observe-decide-review'
@pytest.mark.asyncio
async def test_silent_rail_removes_stream_and_final_prose_but_preserves_tools():
    from types import SimpleNamespace
    rail = runpy.run_path(str(SKILL / 'workflows/silent-rail.py'))['SilentTeamRail']()
    ctx = SimpleNamespace(extra={}, inputs=SimpleNamespace(response=None))
    await rail.before_model_call(ctx)
    await rail.before_model_call(ctx)
    assert len(ctx.extra['_stream_chunk_inspectors']) == 1
    tools = [{'name': 'swarmflow', 'arguments': {'args': 'observe'}}]
    chunk = SimpleNamespace(content='starting observe', reasoning_content='internal reasoning', tool_calls=tools)
    for inspector in ctx.extra['_stream_chunk_inspectors']:
        await inspector(ctx, chunk)
    assert not chunk.content and not chunk.reasoning_content
    assert chunk.tool_calls == tools
    reply = SimpleNamespace(content='final summary', tool_calls=tools)
    ctx.inputs.response = reply
    await rail.after_model_call(ctx)
    assert not reply.content and reply.tool_calls == tools
    ctx.inputs = SimpleNamespace(tool_name='send_message')
    with pytest.raises(RuntimeError, match='forbids send_message'):
        await rail.before_tool_call(ctx)


@pytest.mark.asyncio
async def test_leader_launch_blocks_until_result_without_model_polling():
    from types import SimpleNamespace
    from openjiuwen.agent_teams.harness.async_tools import AsyncToolRuntime
    from openjiuwen.harness.tools.base_tool import ToolOutput
    Rail = runpy.run_path(str(SKILL / 'workflows/silent-rail.py'))['SilentTeamRail']
    injected = []
    async def inject(text):
        injected.append(text)
    runtime = AsyncToolRuntime(inject=inject)
    release = asyncio.Event()
    async def job():
        await release.wait()
        return {'phase': 'observed', 'event_id': '00001'}
    runtime.launch('t', job, tool_name='swarmflow', description='analysis')
    rail = Rail(role='leader', lean=True)
    rail.init(SimpleNamespace(async_tool_runtime=runtime))
    rail.record = lambda _: None
    output = ToolOutput(success=True, data={'task_id': 't', 'status': 'launched'})
    ctx = SimpleNamespace(inputs=SimpleNamespace(tool_name='swarmflow', tool_result=output,
                                                tool_msg=SimpleNamespace(content='launched')))
    waiter = asyncio.create_task(rail.after_tool_call(ctx))
    await asyncio.sleep(0)
    assert not waiter.done() and rail.pending == 't'
    with pytest.raises(RuntimeError, match='model call while'):
        await rail.before_model_call(SimpleNamespace())
    release.set()
    await asyncio.wait_for(waiter, 2)
    assert rail.pending is None
    assert json.loads(ctx.inputs.tool_msg.content)['phase'] == 'observed'
    assert not injected


@pytest.mark.asyncio
async def test_blocking_failure_stops_without_replay_or_completion_message():
    from types import SimpleNamespace
    from openjiuwen.agent_teams.harness.async_tools import AsyncToolRuntime
    from openjiuwen.harness.tools.base_tool import ToolOutput
    Rail = runpy.run_path(str(SKILL / 'workflows/silent-rail.py'))['SilentTeamRail']
    injected, finished, calls = [], [], []
    async def inject(text):
        injected.append(text)
    runtime = AsyncToolRuntime(inject=inject)
    async def job():
        calls.append(1)
        raise RuntimeError('worker failed')
    runtime.launch('bad', job, tool_name='swarmflow', description='analysis')
    rail = Rail(role='leader', lean=True)
    rail.init(SimpleNamespace(async_tool_runtime=runtime))
    rail.record = lambda _: None
    output = ToolOutput(success=True, data={'task_id': 'bad'})
    ctx = SimpleNamespace(inputs=SimpleNamespace(tool_name='swarmflow', tool_result=output,
                                                tool_msg=SimpleNamespace(content='launched')),
                          request_force_finish=finished.append)
    await rail.after_tool_call(ctx)
    assert calls == [1] and finished == [{'output': ''}]
    assert not injected and output.data['phase'] == 'blocked'
    assert runtime.inject is inject and rail.pending is None


@pytest.mark.asyncio
async def test_leader_initialization_closes_after_first_valid_workflow_call():
    from types import SimpleNamespace
    namespace = runpy.run_path(str(SKILL / 'workflows/silent-rail.py'))
    rail = namespace['SilentTeamRail'](role='leader', lean=True)
    assert 'skill_tool' in rail.allowed_tools()
    await rail.before_tool_call(SimpleNamespace(inputs=SimpleNamespace(tool_name='swarmflow', tool_args={
        'script_path': str(SKILL / 'workflows/leader-call.py'), 'args': 'observe'})))
    assert rail.allowed_tools() == {'swarmflow'}
    with pytest.raises(RuntimeError, match='outside leader boundary'):
        await rail.before_tool_call(SimpleNamespace(inputs=SimpleNamespace(tool_name='mcp_career-emulator_take_action', tool_args={})))


@pytest.mark.asyncio
@pytest.mark.parametrize('boundary', ['pending_model', 'message', 'cross_metric'])
async def test_real_callback_dispatcher_cannot_swallow_boundary_and_execute(boundary):
    from types import SimpleNamespace
    from uuid import uuid4
    from openjiuwen.core.single_agent.agent_callback_manager import AgentCallbackManager
    from openjiuwen.core.single_agent.rail.base import (
        AgentCallbackContext, AgentCallbackEvent, ModelCallInputs, ToolCallInputs, rail as wrap_rail,
    )
    Rail = runpy.run_path(str(SKILL / 'workflows/silent-rail.py'))['SilentTeamRail']
    guard = Rail(role='decide', lean=True)
    manager = AgentCallbackManager('boundary-' + uuid4().hex)
    agent = SimpleNamespace(agent_callback_manager=manager)
    guard.init(agent)
    executed = []
    if boundary == 'pending_model':
        guard.pending = 'running-worker'
        event, inputs = AgentCallbackEvent.BEFORE_MODEL_CALL, ModelCallInputs()
        callback = guard.before_model_call
    else:
        event = AgentCallbackEvent.BEFORE_TOOL_CALL
        inputs = ToolCallInputs(tool_name='send_message') if boundary == 'message' else ToolCallInputs(
            tool_name='bash', tool_args={'command': f'python3 "{SKILL}/scripts/analyse.py" N ready'})
        callback = guard.before_tool_call
    await manager.register_callback(event, callback)
    @wrap_rail(before=event)
    async def body(self, ctx):
        executed.append(True)
        return 'executed'
    try:
        result = await body(agent, AgentCallbackContext(agent=agent, inputs=inputs))
        assert result == {'output': ''} and not executed
    finally:
        await manager.clear()


@pytest.mark.asyncio
async def test_actual_minimal_builder_includes_role_markdown_once():
    from types import SimpleNamespace
    from openjiuwen.harness.prompts.builder import SystemPromptBuilder, PromptMode
    from openjiuwen.core.single_agent.prompts.builder import PromptSection
    builder=SystemPromptBuilder(mode=PromptMode.MINIMAL)
    builder.add_section(PromptSection('identity',{'cn':'worker'}))
    builder.add_section(PromptSection('safety',{'cn':'safety'}))
    builder.add_section(PromptSection('skills',{'cn':'read generic skill first'}))
    rail=runpy.run_path(str(SKILL/'workflows/silent-rail.py'))['SilentTeamRail'](role='decide',lean=True)
    rail.init(SimpleNamespace(system_prompt_builder=builder))
    ctx=SimpleNamespace(inputs=SimpleNamespace(tools=[{'name':'bash'}]),extra={})
    await rail.before_model_call(ctx)
    first=builder.build()
    await rail.before_model_call(ctx)
    assert builder.build()==first
    assert '# 决策与动作' in first and 'read-context.py' in first
    assert 'safety' in first and 'read generic skill first' not in first


