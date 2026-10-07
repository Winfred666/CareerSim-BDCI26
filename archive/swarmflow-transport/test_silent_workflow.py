"""Exercise persisted questions, numeric-only decisions and the real fork/join engine."""
import asyncio
import json
from pathlib import Path
import runpy
import shutil

import pytest

SKILL = Path(__file__).resolve().parents[1] / 'solution/skills/observe-decide-review'


@pytest.fixture
def runtime(tmp_path):
    root = tmp_path / 'skill'
    shutil.copytree(SKILL, root)
    notes = root / 'notebooks'
    (notes / 'session.json').write_text('{"session_id":"game"}')
    observation = {'current_state': {'session_id': 'game', 'time': {'current_month': 1},
                   'status': dict(level='L1', output=2, skill=6, network=3, health=5, dignity=5, wealth=4)},
                   'current_event': {'title': 'PRIVATE TITLE', 'description': 'PRIVATE STORY'},
                   'choices': [{'choice': 1, 'action': 'PRIVATE ACTION', 'description': 'PRIVATE DETAIL'}], 'events': ''}
    path = tmp_path / 'observation.json'
    path.write_text(json.dumps(observation))
    state = runpy.run_path(str(root / 'scripts/translation-state.py'))
    refresh = runpy.run_path(str(root / 'scripts/refresh-redline.py'))
    context = runpy.run_path(str(root / 'scripts/read-context.py'))
    return root, notes, observation, path, state, refresh, context


def complete(state, metric, notes):
    result = state['answer'](metric, 'ready', notes)
    for _ in range(80):
        if result['complete']:
            return result
        if metric == 'O':
            question = result['question']
            answer = '3' if '主要发生' in question else '2' if '直接处理哪类' in question else '0'
        else:
            ids = [str(c['choice']) for c in state['current_task'](notes)['choices']]
            answer = json.dumps({k: 0 if metric in ('N', 'R') else {m: 0 for m in metric} for k in ids})
        result = state['answer'](metric, answer, notes)
    raise AssertionError('questionnaire did not terminate')


def test_ready_has_public_event_and_first_question_without_advancing(runtime):
    _, notes, observation, path, state, refresh, _ = runtime
    refresh['refresh_observation'](notes, path, emit=False)
    for metric in ('O', 'N', 'S', 'HW', 'R'):
        first = state['answer'](metric, 'ready', notes)
        if metric == 'R':
            assert observation['current_event']['description'] in first['question']
            continue
        assert first['event']['current_event'] == observation['current_event']
        assert first['event']['choices'][0]['action'] == 'PRIVATE ACTION'
        assert first['event']['choices'][0]['description'] == 'PRIVATE DETAIL'
        assert first['question'] and not first['complete']
        assert first == state['answer'](metric, 'ready', notes)
    assert '1.' in state['answer']('O', 'ready', notes)['question']
    assert '当前行动' in state['answer']('S', 'ready', notes)['question']


def test_decide_requires_all_metrics_and_never_returns_event_text(runtime):
    _, notes, _, path, state, refresh, context = runtime
    refresh['refresh_observation'](notes, path, emit=False)
    for metric in 'ON':
        complete(state, metric, notes)
    with pytest.raises(ValueError, match='incomplete: S'):
        context['read_context'](notebooks=notes)
    for metric in ('S', 'HW', 'R'):
        complete(state, metric, notes)
    result = context['read_context']('00001', notebooks=notes)
    assert result['options'] == [{'choice': 1, 'metrics': dict(O=0, N=0, S=0, H=0, W=0, R=0),
                                  'source': 'estimated', 'selectable': True}]
    assert 'PRIVATE' not in json.dumps(result)
    assert result['untranslated_metrics'] == []
    assert result['ignored_metrics'] == ['D']
    with pytest.raises(ValueError, match='stale event_id'):
        context['read_context']('00002', notebooks=notes)


def test_official_menu_bypasses_questions_and_preserves_cost(runtime):
    _, notes, observation, path, state, refresh, context = runtime
    observation['choices'] = [{'choice': 3, 'action': '保留体力',
                              'status_updates': {'Skill': 2, 'Output': -1}, 'energy_cost': 0}]
    path.write_text(json.dumps(observation))
    refresh['refresh_observation'](notes, path, emit=False)
    assert all(state['answer'](m, 'ready', notes)['complete'] for m in ('O', 'N', 'S', 'HW', 'R'))
    option = context['read_context'](notebooks=notes)['options'][0]
    assert option == {'choice': 3, 'metrics': dict(O=-1, N=0, S=2, H=0, W=0, R=0), 'source': 'official',
                      'selectable': False, 'energy_cost': 0}


def test_wrong_version_and_wrong_observation_receipts_are_rejected(runtime):
    root, notes, _, path, state, refresh, context = runtime
    refresh['refresh_observation'](notes, path, emit=False)
    for metric in ('O', 'N', 'S', 'HW', 'R'):
        complete(state, metric, notes)
    directory = state['event_dir'](notes, state['current_task'](notes))
    receipt = directory / 'N/complete.json'
    original = json.loads(receipt.read_text())
    receipt.write_text(json.dumps({**original, 'observation_hash': 'stale'}))
    with pytest.raises(ValueError, match='receipt mismatch'):
        context['read_context'](notebooks=notes)
    receipt.write_text(json.dumps(original))
    script = root / 'scripts/questionnaires/O.py'
    script.write_text(script.read_text() + '\n# changed\n')
    with pytest.raises(ValueError, match='questionnaire changed'):
        state['answer']('O', 'ready', notes)


@pytest.mark.asyncio
@pytest.mark.parametrize('cap', [1, 5])
async def test_real_engine_waits_for_all_workers_and_control_only_completion_is_valid(runtime, cap):
    from openjiuwen.agent_teams.workflow.engine.backends.base import AgentBackend, AgentResult
    from openjiuwen.agent_teams.workflow.engine.runner import run_workflow
    root, notes, _, path, state, refresh, context = runtime
    started, finished = [], []
    class Backend(AgentBackend):
        async def run(self, prompt, opts, schema_json):
            role = opts['label']
            started.append(role)
            if role == 'observe':
                refresh['refresh_observation'](notes, path, emit=False)
            elif role.startswith('analyse_'):
                await asyncio.sleep({'O': .03, 'N': .01, 'S': .02, 'HW': .04, 'R': .05}[role[8:]])
                complete(state, role[8:], notes)
                with pytest.raises(ValueError, match='barrier'):
                    context['read_context'](notebooks=notes)
            finished.append(role)
            return AgentResult(text='', structured={})
    backend = Backend()
    async def call(stage):
        return await asyncio.wait_for(run_workflow(str(root / 'workflows/leader-call.py'),
                                                  args=stage, backend=backend, cap=cap, strict=True), 10)
    assert (await call('observe'))['phase'] == 'observed'
    result = await call('analyse')
    assert result['phase'] == 'analysed'
    assert set(finished) == {'observe', 'analyse_O', 'analyse_N', 'analyse_S', 'analyse_HW', 'analyse_R'}
    assert context['read_context'](notebooks=notes)['event_id'] == '00001'
    with pytest.raises(ValueError, match='blocked'):
        await call('analyse')


@pytest.mark.asyncio
async def test_worker_early_exit_does_not_release_decide(runtime):
    from openjiuwen.agent_teams.workflow.engine.backends.base import AgentBackend, AgentResult
    from openjiuwen.agent_teams.workflow.engine.runner import run_workflow
    root, notes, _, path, state, refresh, _ = runtime
    refresh['refresh_observation'](notes, path, emit=False)
    state['save'](notes / 'workflow-state.json', {'phase': 'observed', 'event_id': '00001'})
    class Backend(AgentBackend):
        async def run(self, prompt, opts, schema_json):
            if opts['label'] != 'analyse_S':
                complete(state, opts['label'][8:], notes)
            return AgentResult(text='', structured={})
    with pytest.raises(ValueError, match='incomplete: S'):
        await run_workflow(str(root / 'workflows/leader-call.py'), args='analyse', backend=Backend())
    with pytest.raises(ValueError, match='blocked'):
        state['begin_stage']('decide', notes)


def test_full_receipt_sequence_and_terminal(runtime):
    root, notes, observation, path, state, refresh, context = runtime
    state['begin_stage']('observe', notes)
    refresh['refresh_observation'](notes, path, emit=False)
    state['finish_stage']('observe', notes)
    state['begin_stage']('analyse', notes)
    for m in ('O', 'N', 'S', 'HW', 'R'):
        complete(state, m, notes)
    state['finish_stage']('analyse', notes)
    state['begin_stage']('decide', notes)
    context['read_context'](notebooks=notes)
    state['mark_acted']('00001', 1, notes)
    state['finish_stage']('decide', notes)
    state['begin_stage']('review', notes)
    record = runpy.run_path(str(root / 'scripts/record-review.py'))['record']
    record(notes, {'Skill': 1}, 1)
    assert state['finish_stage']('review', notes)['phase'] == 'reviewed'
    state['begin_stage']('observe', notes)
    path.write_text(json.dumps({'current_state': observation['current_state'],
                               'ending_score': {'completed': True, 'survival_months': 48}}))
    refresh['refresh_observation'](notes, path, emit=False)
    assert state['finish_stage']('observe', notes)['phase'] == 'terminal'
    with pytest.raises(ValueError, match='blocked'):
        state['begin_stage']('analyse', notes)


@pytest.mark.asyncio
async def test_framework_does_not_replay_failed_worker(runtime):
    from openjiuwen.agent_teams.workflow.engine.backends.base import AgentBackend
    from openjiuwen.agent_teams.workflow.engine.runner import run_workflow
    root, notes, _, _, state, _, _ = runtime
    calls = []
    class Backend(AgentBackend):
        async def run(self, prompt, opts, schema_json):
            calls.append(opts['label'])
            raise RuntimeError('interrupted after tool side effect')
    with pytest.raises(RuntimeError, match='observe failed'):
        await run_workflow(str(root / 'workflows/leader-call.py'), args='observe', backend=Backend())
    assert calls == ['observe']
    assert state['read'](notes / 'workflow-state.json')['phase'] == 'observe_running'


def test_question_cursor_is_script_owned_and_optional_internal_replay_guard_remains(runtime):
    _, notes, _, path, state, refresh, _ = runtime
    refresh['refresh_observation'](notes, path, emit=False)
    first = state['answer']('O', 'ready', notes)
    assert 'event_id' not in first and 'question_id' not in first
    lane = state['event_dir'](notes, state['current_task'](notes)) / 'O'
    cursor = state['read'](lane / 'cursor.json')
    state['answer']('O', '3', notes)
    assert cursor != state['read'](lane / 'cursor.json')
    with pytest.raises(ValueError, match='stale question_id'):
        state['answer']('O', '2', notes, cursor['event_id'], cursor['question_id'])


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
        return {'phase': 'analysed', 'event_id': '00001'}
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
    assert json.loads(ctx.inputs.tool_msg.content)['phase'] == 'analysed'
    assert not injected


@pytest.mark.asyncio
async def test_analysis_boundary_exposes_two_tools_and_rejects_cross_metric(runtime):
    from types import SimpleNamespace
    root, *_ = runtime
    Rail = runpy.run_path(str(root / 'workflows/silent-rail.py'))['SilentTeamRail']
    rail = Rail(role='worker', lean=True)
    rail.init(SimpleNamespace(card=SimpleNamespace(name='wf-test-analyse-o-1')))
    wake = SimpleNamespace(inputs=SimpleNamespace(query='请调用脚本回答新问题\n框架冗余结束提醒'))
    await rail.before_invoke(wake)
    assert wake.inputs.query == '请调用脚本回答新问题'
    ctx = SimpleNamespace(extra={}, inputs=SimpleNamespace(tools=[{'name': name} for name in
                           ('bash', 'structured_output', 'read_file', 'swarmflow', 'send_message')]))
    await rail.before_model_call(ctx)
    assert [x['name'] for x in ctx.inputs.tools] == ['bash', 'structured_output']
    async def call(command):
        await rail.before_tool_call(SimpleNamespace(inputs=SimpleNamespace(tool_name='bash', tool_args={'command': command})))
    script = root / 'scripts/analyse.py'
    await call(f'python3 "{script}" O ready')
    with pytest.raises(ValueError, match='metric boundary'):
        await call(f'python3 "{script}" N ready')
    with pytest.raises(ValueError, match='shell chaining'):
        await call(f'python3 "{script}" O ready; true')
    with pytest.raises(ValueError, match='script outside'):
        await call(f'python3 "{root}/scripts/read-context.py"')


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
async def test_lean_prompt_keeps_safety_and_removes_inapplicable_team_protocols():
    from types import SimpleNamespace
    from openjiuwen.core.single_agent.prompts.builder import PromptSection, SystemPromptBuilder
    Rail = runpy.run_path(str(SKILL / 'workflows/silent-rail.py'))['SilentTeamRail']
    builder = SystemPromptBuilder()
    for name in ('safety', 'trusted_dirs_policy', 'team_inbound_tags', 'prompt_attachments',
                 'task_tool', 'todo', 'team_role', 'team_hitt', 'evolution_protocol'):
        builder.add_section(PromptSection(name, {'cn': name}))
    rail = Rail(role='analyse_S', lean=True)
    rail.init(SimpleNamespace(system_prompt_builder=builder))
    ctx = SimpleNamespace(inputs=SimpleNamespace(tools=[{'name': 'bash'}, {'name': 'send_message'}]), extra={})
    await rail.before_model_call(ctx)
    assert set(builder.get_all_sections()) == {
        'safety', 'trusted_dirs_policy', 'team_inbound_tags', 'prompt_attachments', 'identity'}
    assert [x['name'] for x in ctx.inputs.tools] == ['bash']


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
    guard = Rail(role='analyse_O', lean=True)
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
    rail=runpy.run_path(str(SKILL/'workflows/silent-rail.py'))['SilentTeamRail'](role='analyse_HW',lean=True)
    rail.init(SimpleNamespace(system_prompt_builder=builder))
    ctx=SimpleNamespace(inputs=SimpleNamespace(tools=[{'name':'bash'}]),extra={})
    await rail.before_model_call(ctx)
    first=builder.build()
    await rail.before_model_call(ctx)
    assert builder.build()==first
    assert '# analyse_HW' in first and ' HW ready' in first
    assert 'safety' in first and 'read generic skill first' not in first


@pytest.mark.asyncio
async def test_same_answers_get_script_owned_tool_tags_without_cli_ids(runtime):
    from types import SimpleNamespace
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
    root,notes,_,path,state,refresh,_=runtime
    refresh['refresh_observation'](notes,path,emit=False)
    rail=runpy.run_path(str(root/'workflows/silent-rail.py'))['SilentTeamRail'](role='analyse_O',lean=True)
    state['answer']('O','ready',notes)
    tags=[]
    for answer in ('3','2'):
        command=f'python3 "{root}/scripts/analyse.py" O 0'
        call=ToolCall(id='test',type='function',name='bash',arguments=json.dumps({'command':command}))
        ctx=SimpleNamespace(inputs=SimpleNamespace(response=SimpleNamespace(content='',tool_calls=[call]),tools=[]))
        await rail.after_model_call(ctx)
        args=json.loads(call.arguments)
        assert args['command']==command
        tags.append(args['call_goal'])
        state['answer']('O',answer,notes)
    assert tags[0]!=tags[1]
