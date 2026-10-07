"""Leader calls one stage; analyse joins all five silent workers before returning."""
from pathlib import Path
import runpy
from swarmflow import agent, parallel
# This Jiuwen release has no public retry option; disable replay in the current run only.
from openjiuwen.agent_teams.workflow.engine.primitives import _rt

META = {"name": "career-silent-stage", "description": "静默调用一个阶段并验证完成凭据", "resume_scope": "run"}
ROOT = Path(__file__).resolve().parent.parent
STATE = runpy.run_path(str(ROOT / 'scripts/translation-state.py'))


async def invoke(role):
    # Identity travels in the backend label; the rail loads this role's markdown.
    reply = await agent(
        '请调用脚本回答新问题' if role.startswith('analyse_') else '请执行本角色步骤',
        label=role,
        options={"timeout": 600},
        schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    if reply is None:
        raise RuntimeError(f'{role} failed')
    if reply != {}:
        raise RuntimeError(f'{role} did not finish silently')
    return True


async def run(stage):
    _rt.get().retries = 0
    STATE['begin_stage'](stage)
    if stage == 'analyse':
        results = await parallel([
            lambda: invoke('analyse_O'),
            lambda: invoke('analyse_N'),
            lambda: invoke('analyse_S'),
            lambda: invoke('analyse_HW'),
            lambda: invoke('analyse_R'),
        ])
        if results != [True] * 5:
            raise RuntimeError('analysis barrier failed; decide remains blocked')
    else:
        await invoke(stage)
    return STATE['finish_stage'](stage)
