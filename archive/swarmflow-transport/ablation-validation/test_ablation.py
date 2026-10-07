import asyncio,json,runpy,shutil
from pathlib import Path
import pytest
SKILL=Path(__file__).resolve().parents[1]/'skills/observe-decide-review'
@pytest.fixture
def env(tmp_path):
 root=tmp_path/'skill';shutil.copytree(SKILL,root);notes=root/'notebooks'
 (notes/'session.json').write_text('{"session_id":"game"}')
 obs={'current_state':{'session_id':'game','time':{'current_month':1},'status':dict(level='L1',output=2,skill=6,network=3,health=5,dignity=5,wealth=4)},'current_event':{'title':'完整标题','description':'完整场景','extra':'保留额外公开字段'},'choices':[{'choice':1,'action':'行动','description':'细节','energy_cost':1},{'choice':2,'action':'官方菜单','status_updates':{'Skill':2}}],'events':''}
 path=tmp_path/'obs.json';path.write_text(json.dumps(obs))
 mods={name:runpy.run_path(str(root/'scripts'/f'{name}.py')) for name in ['event-state','refresh-redline','read-context','record-review']}
 return root,notes,obs,path,mods

def test_raw_context_and_fixed_hints(env):
 root,notes,obs,path,m=env;s=m['event-state']
 with pytest.raises(ValueError):s['begin_stage']('decide',notes)
 s['begin_stage']('observe',notes);m['refresh-redline']['refresh_observation'](notes,path,emit=False)
 with pytest.raises(ValueError):m['read-context']['read_context'](notebooks=notes)
 s['finish_stage']('observe',notes)
 data=m['read-context']['read_context'](notebooks=notes)
 for key in ['current_state','current_event','choices','events']:assert data[key]==obs[key]
 assert '固定表' in data['promotion_ratio'] and '补短板' in data
 assert 'options' not in data and not (notes/'translations').exists()
 assert not (root/'scripts/questionnaires').exists()
 with pytest.raises(ValueError):s['begin_stage']('analyse',notes)
 with pytest.raises(ValueError):m['read-context']['read_context']('00002',notebooks=notes)

def test_mutated_observation_blocks_decide(env):
 _,notes,obs,path,m=env;s=m['event-state'];s['begin_stage']('observe',notes)
 m['refresh-redline']['refresh_observation'](notes,path,emit=False);s['finish_stage']('observe',notes)
 task=s['current_task'](notes);p=s['event_dir'](notes,task)/'observation.json';data=json.loads(p.read_text());data['choices'][0]['action']='changed';p.write_text(json.dumps(data))
 with pytest.raises(ValueError,match='snapshot mismatch'):s['begin_stage']('decide',notes)

@pytest.mark.asyncio
async def test_real_engine_three_workers_wait_receipts_terminal(env):
 from openjiuwen.agent_teams.workflow.engine.backends.base import AgentBackend,AgentResult
 from openjiuwen.agent_teams.workflow.engine.runner import run_workflow
 root,notes,obs,path,m=env;s=m['event-state'];finished=[]
 class Backend(AgentBackend):
  async def run(self,prompt,opts,schema_json):
   role=opts['label'];assert prompt=='请执行本角色步骤'
   await asyncio.sleep(.01)
   if role=='observe':m['refresh-redline']['refresh_observation'](notes,path,emit=False)
   elif role=='decide':
    assert m['read-context']['read_context'](notebooks=notes)['choices']==obs['choices']
    s['mark_acted']('00001',1,notes)
   elif role=='review':m['record-review']['record'](notes,{'Skill':1},1)
   else:raise AssertionError(role)
   finished.append(role);return AgentResult(text='',structured={})
 async def call(stage):return await asyncio.wait_for(run_workflow(str(root/'workflows/leader-call.py'),args=stage,backend=Backend(),cap=1,strict=True),10)
 for stage,phase in [('observe','observed'),('decide','acted'),('review','reviewed')]:assert (await call(stage))['phase']==phase
 assert finished==['observe','decide','review']
 path.write_text(json.dumps({'current_state':obs['current_state'],'ending_score':{'completed':True}}))
 assert (await call('observe'))['phase']=='terminal'
 with pytest.raises(ValueError):await call('decide')

@pytest.mark.asyncio
async def test_early_worker_completion_does_not_release_decide(env):
 from openjiuwen.agent_teams.workflow.engine.backends.base import AgentBackend,AgentResult
 from openjiuwen.agent_teams.workflow.engine.runner import run_workflow
 root,notes,_,_,m=env
 class Backend(AgentBackend):
  async def run(self,prompt,opts,schema_json):return AgentResult(text='',structured={})
 with pytest.raises(FileNotFoundError):await run_workflow(str(root/'workflows/leader-call.py'),args='observe',backend=Backend())
 with pytest.raises(ValueError):m['event-state']['begin_stage']('decide',notes)
