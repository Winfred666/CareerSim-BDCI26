"""Ordinary teammate dispatch, silent completion and persisted phase barriers."""
import asyncio,json,runpy,shutil
from pathlib import Path
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
import pytest

BASE=Path(__file__).resolve().parents[1]
@pytest.fixture(params=['solution','.career_sim_runner/solution_no_translation'])
def native(request,tmp_path):
 root=tmp_path/'skill';shutil.copytree(BASE/request.param/'skills/observe-decide-review',root)
 return root,runpy.run_path(str(root/'scripts/silent-team.py'))

def test_native_tools_exclude_flow_and_messages(native):
 root,ns=native
 rail=ns['SilentTeamRail'](role='leader',lean=True)
 assert 'create_task' in rail.allowed_tools()
 assert not {'swarmflow','send_message','async_task_output'} & rail.allowed_tools()
 assert 'handbook' in ns['CONTROL']['ROLES'] and not (root/'workflows').exists()
 schema=ns['scoped_tool']({'name':'create_task'})['parameters']['properties']['tasks']['items']
 assert set(schema['properties'])=={'assignee'}
 assert schema['properties']['assignee']['enum']==['handbook']

@pytest.mark.asyncio
async def test_native_wait_requires_task_complete_and_worker_ready(native):
 _,ns=native;rail=ns['SilentTeamRail'](role='leader',lean=True)
 task=NS(status='in_progress',assignee='observe');member=NS(status='busy')
 manager=NS(get=AsyncMock(return_value=task),team_name='test',db=NS(member=NS(get_member=AsyncMock(return_value=member))))
 rail.manager=lambda:manager;rail.record=lambda row:None;rail.pending_stage='observe'
 ns['CONTROL']['finish']=lambda stage:{'phase':'observed'}
 ctx=NS(inputs=NS(tool_name='create_task',tool_result=NS(success=True,data={'task_id':'t'}),tool_msg=NS(content='')),request_force_finish=lambda _:None)
 waiting=asyncio.create_task(rail.after_tool_call(ctx));await asyncio.sleep(.01)
 assert not waiting.done()
 with pytest.raises(RuntimeError,match='model call while'):await rail.before_model_call(NS())
 task.status='completed';await asyncio.sleep(.12);assert not waiting.done()
 member.status='ready';await asyncio.wait_for(waiting,1)
 assert json.loads(ctx.inputs.tool_msg.content)=={'phase':'observed'} and rail.pending is None

@pytest.mark.asyncio
async def test_failed_native_member_stops_without_replaying(native):
 _,ns=native;rail=ns['SilentTeamRail'](role='leader',lean=True)
 rail.manager=lambda:NS(get=AsyncMock(return_value=NS(status='in_progress',assignee='observe')),team_name='test',db=NS(member=NS(get_member=AsyncMock(return_value=NS(status='error')))))
 finished=[];ctx=NS(inputs=NS(tool_name='create_task',tool_result=NS(success=True,data={'task_id':'t'})),request_force_finish=finished.append)
 with pytest.raises(RuntimeError,match='stopped unexpectedly'):await rail.after_tool_call(ctx)
 assert finished==[{'output':''}] and rail.pending is None

@pytest.mark.asyncio
async def test_completion_uses_native_task_id_and_ends_turn(native):
 _,ns=native;rail=ns['SilentTeamRail'](role='observe',lean=True);rail.task_id='owned-task';rail.record=lambda _:None
 call=NS(name='member_complete_task',arguments='{}');ctx=NS(inputs=NS(response=NS(content='report',tool_calls=[call]),tools=[]))
 await rail.after_model_call(ctx);assert json.loads(call.arguments)=={'task_id':'owned-task'}
 ns['CONTROL']['validate_role']=lambda role:None
 ctx.inputs=NS(tool_name='member_complete_task',tool_args={'task_id':'owned-task'})
 await rail.before_tool_call(ctx)
 finished=[];ctx.request_force_finish=finished.append;ctx.inputs.tool_result=NS(success=True)
 await rail.after_tool_call(ctx);assert finished==[{'output':''}]

def test_stage_requires_handbook_receipt_and_exact_parallel_group(native):
 root,ns=native;control=ns['CONTROL'];notes=root/'notebooks';(notes/'session.json').write_text('{"session_id":"s"}')
 def task(role):return {'assignee':role.lower().replace('_','-'),'title':role,'content':'请调用脚本回答新问题' if role.startswith('analyse_') else '请执行本角色步骤'}
 with pytest.raises(FileNotFoundError):control['begin']([task('observe')])
 assert control['begin']([task('handbook')])=='handbook'
 (notes/'handbook-ready.json').write_text('{"session_id":"s"}')
 with pytest.raises(ValueError):control['begin']([task('handbook')])
 assert control['begin']([task('observe')])=='observe'
 with pytest.raises(ValueError):control['begin']([task('decide')])
 if control['ANALYSES']:
  with pytest.raises(ValueError):control['begin']([task('analyse_O')])

def test_setup_native_disables_flow_and_uses_scheduled_dispatch(tmp_path):
 from career_sim_runner.setup import configure_silent_team
 p=tmp_path/'observe-decide-review/scripts/silent-team.py';p.parent.mkdir(parents=True);p.write_text('# fixture')
 data={'modes':{'team':{'t':{'enable_swarmflow':True,'swarmflow_concurrency':{'agents_per_run':5}}}}}
 configure_silent_team(data,{'native_teammates':True,'silent_team':True,'lean_team':True},tmp_path)
 t=data['modes']['team']['t']
 assert t['enable_swarmflow'] is False and t['dispatch_mode']=='scheduled'
 assert 'swarmflow_concurrency' not in t
 assert t['enable_task_verification'] is False
 assert t['agents']['leader']['rails'][-1]['params']['file_path']==str(p)

@pytest.mark.asyncio
async def test_exact_skill_cd_prefix_normalizes_before_boundary(native):
 root,ns=native;rail=ns['SilentTeamRail'](role='observe',lean=True);rail.record=lambda _:None
 call=NS(name='bash',arguments=json.dumps({'command':f'cd "{root}" && python3 scripts/refresh-redline.py /tmp/observation.json'}))
 await rail.after_model_call(NS(inputs=NS(response=NS(content='',tool_calls=[call]),tools=[])))
 args=json.loads(call.arguments)
 assert args['command'].startswith('python3 ') and '&&' not in args['command']
 await rail.before_tool_call(NS(inputs=NS(tool_name='bash',tool_args=args)))

@pytest.mark.asyncio
async def test_native_dispatch_fills_control_fields_without_model_text(native):
 _,ns=native;rail=ns['SilentTeamRail'](role='leader',lean=True);rail.record=lambda _:None
 calls=[NS(name='spawn_teammate',arguments='{"member_name":"decide"}'),
        NS(name='create_task',arguments='{"tasks":[{"assignee":"decide"}]}')]
 await rail.after_model_call(NS(inputs=NS(response=NS(content='',tool_calls=calls),tools=[])))
 assert json.loads(calls[0].arguments)['prompt']=='请按本角色步骤执行。'
 assert json.loads(calls[1].arguments)['tasks']==[{'assignee':'decide','title':'decide','content':'请执行本角色步骤'}]
