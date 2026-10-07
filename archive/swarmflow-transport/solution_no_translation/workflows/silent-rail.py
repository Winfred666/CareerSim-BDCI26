"""Role-scoped tools and a blocking Leader boundary, without model polling."""
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
MCP = 'mcp_career-emulator_'
TOOLS = {
    'observe': {'read_file', 'bash', 'structured_output', MCP + 'observe'},
    'decide': {'bash', 'structured_output', MCP + 'take_action'},
    'review': {'read_file', 'bash', 'structured_output', MCP + 'check_latest_logs'},
    'leader': {'skill_tool', 'read_file', 'bash', 'swarmflow', MCP + 'new_game',
               MCP + 'observe', MCP + 'take_action', MCP + 'show_employee_handbook'},
}
SCRIPTS = {'observe': {'refresh-redline.py'}, 'decide': {'read-context.py', 'workflow-state.py'},
           'review': {'record-review.py'}, 'leader': {'workflow-state.py'}}
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
        'swarmflow': ('调用固定阶段；运行层阻塞直到完成，返回 phase 和 event_id。无需轮询。', {
            'script_path': {'type': 'string', 'description': 'skill 绝对路径 + /workflows/leader-call.py'},
            'args': {'type': 'string', 'enum': ['observe', 'decide', 'review']} }),
        'bash': ('执行角色给定的一条 Python 命令并返回结果。', {'command': {'type': 'string'}}),
        'read_file': ('读取角色给定的文件。', {'file_path': {'type': 'string'}}),
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
        except Exception:
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

    @enforce_boundary
    async def before_invoke(self, ctx):
        if self.role == 'worker':
            name = str(self.agent.card.name)
            match = re.search(r'-(observe|decide|review)-\d+$', name)
            if not match:
                raise ValueError('missing fixed CareerSim role')
            self.role = match[1]
            # Drop the host's redundant structured-output reminder from the wake.
            ctx.inputs.query = '请执行本角色步骤'

    def allowed_tools(self):
        if self.role == 'leader' and self.workflow_started:
            return {'swarmflow'}
        return TOOLS.get(self.role, set())

    @enforce_boundary
    async def before_model_call(self, ctx):
        if self.pending:
            raise RuntimeError('Leader model call while a workflow is running')
        if self.lean:
            allowed = self.allowed_tools()
            ctx.inputs.tools = [scoped_tool(t) for t in (ctx.inputs.tools or []) if tool_name(t) in allowed]
            if not ctx.inputs.tools:
                raise RuntimeError(f'no tools available for {self.role}')
            builder = getattr(self.agent, 'system_prompt_builder', None)
            if builder:
                for name in UNUSED_SECTIONS:
                    builder.remove_section(name)
                role_rule = ('读取 observe-decide-review skill 后，只依序调用固定磁盘 workflow 的 '
                             'observe、decide、review。swarmflow 阻塞至阶段结束，勿轮询或另发消息。'
                             if self.role == 'leader' else
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
            if self.role == 'leader':
                allowed.add(ROOT / 'stages/handbook.md')
            if path not in allowed:
                raise ValueError('file outside role boundary')
        if name == 'swarmflow':
            if set(args) - {'script_path', 'args', 'call_goal'}:
                raise ValueError('Leader may call only the fixed disk workflow')
            if Path(args.get('script_path', '')).resolve() != ROOT / 'workflows/leader-call.py':
                raise ValueError('wrong workflow path')
            if args.get('args') not in ('observe', 'decide', 'review'):
                raise ValueError('invalid stage')
            self.workflow_started = True

    @enforce_boundary
    async def after_tool_call(self, ctx):
        if not self.lean or self.role != 'leader' or ctx.inputs.tool_name != 'swarmflow':
            return
        output = ctx.inputs.tool_result
        data = getattr(output, 'data', None)
        if not getattr(output, 'success', False) or not isinstance(data, dict) or not data.get('task_id'):
            ctx.request_force_finish({'output': ''})
            return
        runtime = self.agent.async_tool_runtime
        task_id = data['task_id']
        record = runtime.get(task_id)
        if record is None:
            raise RuntimeError('launched workflow has no runtime record')
        # Suppress the async host's second completion injection. This tool call
        # itself delivers the result; no follow-up turn or LLM polling is needed.
        record.format_completed = lambda _: ''
        record.format_failed = lambda _: ''
        original_inject = runtime.inject
        async def inject_nonempty(text):
            if text:
                await original_inject(text)
        runtime.inject = inject_nonempty
        self.pending = task_id
        started = time.monotonic()
        try:
            while record.status == 'running':
                record = await runtime.wait(task_id, 60)
                if record is None:
                    raise RuntimeError('workflow record disappeared while waiting')
            result = record.result if record.status == 'completed' else {'phase': 'blocked', 'error': record.error}
            packet = {'task_id': task_id, **result}
            output.data = packet
            if ctx.inputs.tool_msg is not None:
                ctx.inputs.tool_msg.content = json.dumps(packet, ensure_ascii=False)
            self.record({'role': 'leader', 'wait_task': task_id, 'seconds': round(time.monotonic()-started, 3),
                         'phase': result.get('phase'), 'model_calls_during_wait': 0})
            if result.get('phase') in ('blocked', 'terminal'):
                ctx.request_force_finish({'output': ''})
        finally:
            self.pending = None
            if runtime.inject is inject_nonempty:
                runtime.inject = original_inject
