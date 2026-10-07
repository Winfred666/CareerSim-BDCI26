"""Role-scoped tools and a blocking Leader boundary, without model polling."""
import asyncio
import json
import os
from copy import deepcopy
from functools import wraps
from pathlib import Path
import re
import runpy
import shlex
import time

from openjiuwen.core.single_agent.rail.base import AgentRail
from openjiuwen.core.single_agent.prompts.builder import PromptSection

ROOT = Path(__file__).resolve().parent.parent
STATE = runpy.run_path(str(ROOT / 'scripts/event-state.py'))
CONTROL = runpy.run_path(str(ROOT / 'scripts/team-state.py'))
MCP = 'mcp_career-emulator_'
TOOLS = {
    'observe': {'read_file', 'bash', 'member_complete_task', MCP + 'observe'},
    'decide': {'bash', 'member_complete_task', MCP + 'take_action'},
    'review': {'read_file', 'bash', 'member_complete_task', MCP + 'check_latest_logs'},
    'handbook': {'read_file', 'bash', 'member_complete_task', MCP + 'show_employee_handbook', MCP + 'observe', MCP + 'take_action'},
    'leader': {'skill_tool', 'read_file', 'bash', 'build_team', 'spawn_teammate', 'create_task', MCP + 'new_game'},
}
SCRIPTS = {'observe': {'refresh-redline.py'}, 'decide': {'read-context.py', 'workflow-state.py'},
           'review': {'record-review.py'}, 'leader': {'workflow-state.py'}, 'handbook': {'handbook-state.py'}}
# These host features are not used by the fixed workflow and otherwise instruct
# workers to create tasks, send reports or evolve skills. Keep safety, attachment,
# provenance and filesystem boundary sections intact.
UNUSED_SECTIONS = {'task_tool', 'todo', 'evolution_protocol', 'evolution_team_protocol',
                   'team_role', 'team_hitt', 'team_workflow', 'team_dispatch', 'team_lifecycle',
                   'team_workspace_report_paths', 'response', 'browser_tool_policy'}


def arguments(value):
    return json.loads(value) if isinstance(value, str) else value


def tool_name(tool):
    if isinstance(tool, dict):
        return (tool.get('function') or tool).get('name')
    return getattr(tool, 'name', None)


def scoped_tool(tool):
    """Expose the actual synchronous contract, without generic async/code options."""
    name = tool_name(tool)
    definitions = {
        'build_team': ('建立普通 CareerSim 团队。', {}),
        'spawn_teammate': ('创建一个固定角色，共享工作目录。', {
            'member_name': {'type': 'string', 'enum': list(CONTROL['MEMBERS'])}}),
        'create_task': ('分派当前阶段；所有成员完成并停止执行后返回。', {
            'tasks': {'type': 'array', 'minItems': 1, 'items': {
                'type': 'object', 'properties': {
                    'assignee': {'type': 'string', 'enum': CONTROL['next_members']()}},
                'required': ['assignee'], 'additionalProperties': False}}}),
        'member_complete_task': ('完成当前任务；运行层校验本角色结果并结束本次执行。', {}),
        'bash': ('执行角色给定的一条命令，直接以 python3 开头，使用绝对脚本路径。', {'command': {'type': 'string'}}),
        'read_file': ('读取角色给定的文件。', {'file_path': {'type': 'string', 'enum': [str(ROOT / 'notebooks/session.json')]}}),
    }
    if name not in definitions:
        return tool
    description, properties = definitions[name]
    parameters = {'type': 'object', 'properties': {**properties, 'call_goal': {'type': 'string'}},
                  'required': list(properties), 'additionalProperties': False}
    result = deepcopy(tool)
    if isinstance(result, dict):
        target = result.get('function', result)
        target.update(description=description, parameters=parameters)
    else:
        result.description, result.parameters = description, parameters
    return result


def enforce_boundary(hook):
    # Jiuwen's callback dispatcher logs ordinary exceptions and continues.
    # A force-finish request is therefore essential to skip the model/tool body.
    @wraps(hook)
    async def guarded(self, ctx, *args):
        try:
            return await hook(self, ctx, *args)
        except Exception as exc:
            self.record({'role': self.role, 'boundary_error': str(exc), 'time': time.time()})
            if self.lean and getattr(self.agent, 'ability_manager', None) is not None and not (STATE['NOTEBOOKS'] / 'team-blocked.json').exists():
                STATE['save'](STATE['NOTEBOOKS'] / 'team-blocked.json', {'role': self.role, 'error': str(exc)})
            finish = getattr(ctx, 'request_force_finish', None)
            if finish:
                finish({'output': ''})
            raise
    return guarded


class SilentTeamRail(AgentRail):
    # Run after context/prompt rails so the final model window has only role tools.
    priority = -1000

    def __init__(self, role=None, lean=False, restore=None):
        self.role = role
        self.lean = lean
        self.pending = None
        self.agent = None
        self.started = None
        self.workflow_started = False
        self.base_identity = None

    def init(self, agent):
        self.agent = agent

    def native_tool(self, name):
        from openjiuwen.core.runner import Runner
        card = self.agent.ability_manager.get(name)
        if card is None:
            raise RuntimeError('missing native teammate tool: ' + name)
        return Runner.resource_mgr.get_tool(tool_id=card.id)

    def manager(self):
        name = 'create_task' if self.role == 'leader' else 'member_complete_task'
        return self.native_tool(name).task_manager

    @enforce_boundary
    async def before_invoke(self, ctx):
        if self.role == 'worker':
            member = self.manager().member_name
            self.role = CONTROL['role_for_member'](member)
        if self.role != 'leader':
            tasks = await self.manager().get_tasks_by_assignee(self.manager().member_name, status='in_progress')
            if len(tasks) != 1:
                ctx.request_force_finish({'output': ''})
                return
            self.task_id = tasks[0].task_id
            ctx.inputs.query = ('请调用脚本回答新问题' if self.role.startswith('analyse_') else '请执行本角色步骤')

    def allowed_tools(self):
        if self.role == 'leader' and self.workflow_started:
            return {'create_task'}
        return TOOLS.get(self.role, set())

    @enforce_boundary
    async def before_model_call(self, ctx):
        if self.lean and (STATE['NOTEBOOKS'] / 'team-blocked.json').exists():
            raise RuntimeError('team stopped after a failed stage')
        if self.pending:
            raise RuntimeError('Leader model call while teammates are running')
        if self.lean:
            allowed = self.allowed_tools()
            ctx.inputs.tools = [scoped_tool(t) for t in (ctx.inputs.tools or []) if tool_name(t) in allowed]
            if not ctx.inputs.tools:
                raise RuntimeError(f'no tools available for {self.role}')
            builder = getattr(self.agent, 'system_prompt_builder', None)
            if builder:
                for name in UNUSED_SECTIONS:
                    builder.remove_section(name)
                role_rule = ((ROOT / 'SKILL.md').read_text().split('# CareerSim Leader', 1)[1] if self.role == 'leader' else
                             (ROOT / 'stages' / (self.role + '.md')).read_text().replace('<skill_dir>', str(ROOT)))
                # DeepAgent minimal mode filters out arbitrary section names.
                # Install role markdown in identity, which is actually rendered.
                if self.base_identity is None:
                    section = builder.get_section('identity')
                    self.base_identity = section.render(builder.language) if section else ''
                if self.role != 'leader':
                    builder.remove_section('skills')
                builder.add_section(PromptSection('identity', {'cn': self.base_identity + '\n\n' + role_rule +
                    '\n所有阶段静默，无说明、汇报或总结；失败立即停止，不重放动作。'}, priority=10))
            self.started = time.monotonic()
        inspectors = ctx.extra.setdefault('_stream_chunk_inspectors', [])
        if isinstance(inspectors, dict):
            inspectors = list(inspectors.values())
        ctx.extra['_stream_chunk_inspectors'] = [
            item for item in inspectors if getattr(item, '__self__', None) is not self
        ] + [self.silence_chunk]

    async def silence_chunk(self, ctx, chunk):
        chunk.content = ''
        if hasattr(chunk, 'reasoning_content'):
            chunk.reasoning_content = ''

    async def after_model_call(self, ctx):
        response = getattr(ctx.inputs, 'response', None)
        if response is None:
            return
        response.content = ''
        for call in getattr(response, 'tool_calls', None) or []:
            name = tool_name(call)
            if not isinstance(call, dict) and name in ('create_task', 'spawn_teammate', 'build_team'):
                args = arguments(call.arguments)
                if name == 'create_task':
                    for task in args['tasks']:
                        role = CONTROL['role_for_member'](task['assignee'])
                        task.clear()
                        task.update(assignee=role.lower().replace('_', '-'), title=role,
                                    content='请调用脚本回答新问题' if role.startswith('analyse_') else '请执行本角色步骤')
                elif name == 'spawn_teammate':
                    role = CONTROL['role_for_member'](args['member_name'])
                    args = dict(member_name=role.lower().replace('_', '-'), display_name=role, desc=role,
                                prompt='请按本角色步骤执行。')
                else:
                    args = dict(display_name='CareerSim', team_desc='CareerSim', leader_display_name='leader',
                                leader_desc='分派阶段', enable_hitt=False, enable_task_verification=False)
                call.arguments = json.dumps(args, ensure_ascii=False)
            if tool_name(call) == 'bash' and not isinstance(call, dict):
                args = arguments(call.arguments)
                command = args.get('command', '')
                argv = shlex.split(command)
                # Normalize only an exact skill-directory prefix; never execute cd or a second command.
                if len(argv) >= 5 and argv[0] == 'cd' and Path(argv[1]).resolve() == ROOT and argv[2:4] == ['&&', 'python3']:
                    target = Path(argv[4])
                    if not target.is_absolute():
                        target = ROOT / target
                    args['command'] = shlex.join(['python3', str(target.resolve()), *argv[5:]])
                    call.arguments = json.dumps(args, ensure_ascii=False)
            if tool_name(call) == 'member_complete_task':
                call.arguments = json.dumps({'task_id': self.task_id})
        if self.lean:
            usage = getattr(response, 'usage_metadata', None)
            if hasattr(usage, 'model_dump'):
                usage = usage.model_dump()
            if not isinstance(usage, dict):
                usage = {}
            builder = getattr(self.agent, 'system_prompt_builder', None)
            row = {'role': self.role, 'time': time.time(),
                   'seconds': round(time.monotonic() - self.started, 3) if self.started else None,
                   'tools': [tool_name(t) for t in (ctx.inputs.tools or [])],
                   'usage': usage,
                   'prompt_sections': {name: sec.char_count(builder.language) for name, sec in
                                       builder.get_all_sections().items()} if builder else {}}
            self.record(row)

    @staticmethod
    def record(row):
        # Local telemetry, never sent to the model. One atomic append per call.
        path = ROOT / 'notebooks/runtime-usage.jsonl'
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, (json.dumps(row, ensure_ascii=False, default=str) + '\n').encode())
        finally:
            os.close(fd)

    @enforce_boundary
    async def before_tool_call(self, ctx):
        name = getattr(ctx.inputs, 'tool_name', '')
        if name == 'send_message':
            raise RuntimeError('silent team forbids send_message')
        if not self.lean:
            return
        if self.pending or name not in self.allowed_tools():
            raise RuntimeError(f'tool outside {self.role} boundary: {name}')
        args = arguments(ctx.inputs.tool_args) or {}
        if name == 'bash':
            command = args.get('command', '')
            if any(c in command for c in ('$', '`', '\n')):
                raise ValueError('only one literal role command is allowed')
            lexer = shlex.shlex(command, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            if any(token in (";", "&&", "||", "|", ">", ">>", "<", "&", "(", ")") for token in lexer):
                raise ValueError("shell chaining is outside role boundary")
            argv = shlex.split(command)
            if len(argv) < 2 or argv[0] != 'python3':
                raise ValueError('only the assigned Python script is allowed')
            target = Path(argv[1]).resolve()
            scripts = SCRIPTS.get(self.role, set())
            if target.parent != ROOT / 'scripts' or target.name not in scripts:
                raise ValueError('script outside role boundary')
            if self.role == 'observe' and len(argv) != 3:
                raise ValueError('refresh requires one observation path')
            elif self.role == 'handbook' and len(argv) != 3:
                raise ValueError('handbook requires the final observation path')
            elif self.role == 'review' and len(argv) != 4:
                raise ValueError('review requires delta and choice')
            elif target.name == 'read-context.py' and len(argv) not in (2, 3):
                raise ValueError('invalid context command')
            elif target.name == 'workflow-state.py':
                expected = 'session' if self.role == 'leader' else 'acted'
                if len(argv) != 4 or argv[2] != expected:
                    raise ValueError('state command outside role boundary')
        if name == 'read_file':
            path = Path(args.get('file_path', '')).resolve()
            allowed = {ROOT / 'notebooks/session.json'}
            if self.role == 'handbook':
                allowed.add(ROOT / 'stages/handbook.md')
            if path not in allowed:
                raise ValueError('file outside role boundary')
        if name == 'spawn_teammate':
            role = CONTROL['role_for_member'](args.get('member_name', ''))
            if args.get('display_name') != role or args.get('prompt') != '请按本角色步骤执行。':
                raise ValueError('use the fixed teammate identity and short prompt')
            if any(key in args for key in ('isolation', 'model_name', 'permissions')):
                raise ValueError('use the shared workspace and configured Player model')
        if name == 'create_task':
            self.pending_stage = CONTROL['begin'](args.get('tasks', []))
            self.workflow_started = True
        if name == 'member_complete_task':
            if args.get('task_id') != self.task_id or args.get('note'):
                raise ValueError('invalid completion')
            CONTROL['validate_role'](self.role)

    @enforce_boundary
    async def after_tool_call(self, ctx):
        name = ctx.inputs.tool_name
        output = ctx.inputs.tool_result
        if not self.lean:
            return
        if name == 'member_complete_task':
            if not getattr(output, 'success', False):
                raise RuntimeError('native task completion failed')
            ctx.request_force_finish({'output': ''})
            return
        if self.role != 'leader' or name != 'create_task':
            return
        if not getattr(output, 'success', False):
            raise RuntimeError('native task creation failed')
        data = output.data
        briefs = data.get('tasks', [data])
        task_ids = [row['task_id'] for row in briefs]
        self.pending = task_ids
        started = time.monotonic()
        manager = self.manager()
        try:
            async with asyncio.timeout(600):
                while True:
                    if (STATE['NOTEBOOKS'] / 'team-blocked.json').exists():
                        raise RuntimeError('teammate failed; stage stopped')
                    tasks = [await manager.get(t) for t in task_ids]
                    if any(t is None or t.status in ('cancelled', 'failed') for t in tasks):
                        raise RuntimeError('native teammate task failed')
                    members = [await manager.db.member.get_member(t.assignee, manager.team_name) for t in tasks]
                    if any(m is None or m.status in ('error', 'stopped', 'shut_down') for m in members):
                        raise RuntimeError('native teammate stopped unexpectedly')
                    if all(t.status == 'completed' for t in tasks) and all(m.status == 'ready' for m in members):
                        break
                    await asyncio.sleep(.1)
            packet = CONTROL['finish'](self.pending_stage)
            output.data = packet
            if ctx.inputs.tool_msg is not None:
                ctx.inputs.tool_msg.content = json.dumps(packet, ensure_ascii=False)
            self.record({'role': 'leader', 'native_tasks': task_ids, 'seconds': round(time.monotonic()-started,3),
                         'phase': packet['phase'], 'model_calls_during_wait': 0})
            if packet['phase'] == 'terminal':
                ctx.request_force_finish({'output': ''})
        finally:
            self.pending = None
