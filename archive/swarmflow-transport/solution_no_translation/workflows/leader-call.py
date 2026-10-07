"""Leader calls one stage; waits for one silent worker before returning."""
from pathlib import Path
import runpy
from swarmflow import agent
# This Jiuwen release has no public retry option; disable replay in the current run only.
from openjiuwen.agent_teams.workflow.engine.primitives import _rt

META = {"name": "career-silent-stage", "description": "静默调用一个阶段并验证完成凭据", "resume_scope": "run"}
ROOT = Path(__file__).resolve().parent.parent
STATE = runpy.run_path(str(ROOT / 'scripts/event-state.py'))


async def invoke(role):
    # Identity travels in the backend label; the rail loads this role's markdown.
    reply = await agent(
        '请执行本角色步骤',
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
    await invoke(stage)
    return STATE['finish_stage'](stage)
