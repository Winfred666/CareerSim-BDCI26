"""Bare Leader requests cannot receive hidden effects or numerical preferences."""
import asyncio
from types import SimpleNamespace

import pytest

from scripts.benchmark_bare_correction import AnswerCache, request_for, usage_summary, report, save
from scripts.benchmark_symbolic_policy import correction_packet


def packet():
    return correction_packet(dict(current_event=dict(title='事件',description='原题',doc_id='hidden-id'),
        choices=[dict(choice=1,action='选一',description='描述',status_updates={'HiddenRisk':-2}),
                 dict(choice=2,action='选二',description='描述二')]),
        {'守红线':'“身心健康”过低，禁止选预测削减这些指标的选项（-1也不行）',
         '补短板':'“人脉”差2分晋升'},
        [dict(description='前情',current_month=4,selected_choice={'action':'前次行动'})])


def response(choice=2):
    import json
    raw=dict(role='assistant',tool_calls=[dict(id='x',type='function',function={
        'name':'take_action','arguments':json.dumps({'choice':choice,'notes':'简短行为理由'})})])
    message=SimpleNamespace(tool_calls=[SimpleNamespace(function=SimpleNamespace(
        name='take_action',arguments=raw['tool_calls'][0]['function']['arguments']))],
        model_dump=lambda **kwargs:raw)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)],usage=None)


class Client:
    def __init__(self):
        self.calls=[]
        self.chat=SimpleNamespace(completions=self)
    async def create(self,**request):
        self.calls.append(request)
        await asyncio.sleep(0)
        return response()


def test_payload_keeps_public_history_and_state_only_constraints():
    import json
    p=packet();request=request_for(p,'测试 Leader')
    payload=json.loads(request['messages'][1]['content'])
    assert payload==p
    assert set(p)=={'current_event','options','constraints','event_history'}
    assert '禁止选择会削减这些指标的行为' in p['constraints']['守红线']
    assert p['event_history']['steps'][0]['selected_choice']['action']=='前次行动'
    content=request['messages'][1]['content']
    assert not any(word in content for word in ['HiddenRisk','status_updates','doc_id','推荐选项','reason','confidence'])
    p['options'][0]['metrics']={'R':-1}
    with pytest.raises(ValueError,match='Private menu'):
        request_for(p,'测试 Leader')


@pytest.mark.asyncio
async def test_each_context_has_one_answer_reused_even_if_action_is_wrong(tmp_path):
    client=Client();cache=AnswerCache(tmp_path,client,'测试 Leader')
    context={'time':{'current_month':5},'status':{'health':3}}
    # The evaluator may judge 2 wrong; it is still accepted and never re-asked.
    a,b=await asyncio.gather(cache(packet(),context),cache(packet(),context))
    assert a==b and a['choice']==2 and len(client.calls)==1
    assert len(list(tmp_path.glob('*.json')))==1
    c=await AnswerCache(tmp_path,Client(),'测试 Leader')(packet(),context)
    assert c==a
    other={'time':{'current_month':47},'status':{'health':3}}
    d=await cache(packet(),other)
    assert d['cache_key']!=a['cache_key'] and len(client.calls)==2
    assert 'public_cache_context' not in client.calls[0]
    assert usage_summary(tmp_path)['unknown_usage_attempts']==2


@pytest.mark.asyncio
async def test_error_retries_are_bounded_without_semantic_retries(tmp_path):
    class Broken(Client):
        async def create(self,**request):
            self.calls.append(request)
            raise TimeoutError()
    client=Broken();cache=AnswerCache(tmp_path,client,'测试 Leader')
    with pytest.raises(RuntimeError,match='Two unusable'):
        await cache(packet(),{})
    assert len(client.calls)==2
    with pytest.raises(RuntimeError,match='Two unusable'):
        await cache(packet(),{})
    assert len(client.calls)==2


@pytest.mark.asyncio
async def test_correction_hook_sees_no_gold_or_translations_and_keeps_action_evidence():
    from pathlib import Path
    from scripts.benchmark_symbolic_policy import load_inputs, rollout
    root=Path(__file__).resolve().parents[1]
    scripts=root/'solution/skills/observe-decide-review/scripts'
    policy,helpers,errors=load_inputs(scripts/'numeric-policy.py',scripts/'read-context.py',
        root/'logs/conditional_model-coherent-20261005.json',root/'logs/conditional_current.py')
    # Force only the dispatch gate so one deterministic full rollout verifies
    # the bridge. The public-only chooser receives no private parameters.
    helpers['confidence']=lambda *args:{'reason':'test route','samples':0,'confidence':0}
    packets=[]
    async def choose(public,context):
        import json
        text=json.dumps(public,ensure_ascii=False)
        assert not any(k in text for k in ('actual_deltas','metrics','confidence','test route','推荐选项'))
        assert 'session_id' not in context
        assert not any('hidden' in k.lower() for k in context.get('status',{}))
        packets.append(public)
        return dict(choice=public['options'][0]['choice'],notes='测试',cache_key='test')
    result=await rollout(policy,helpers,errors,'public-correction-bridge',0,'calibrated',True,True,
                         correction=choose)
    assert packets and len(result['corrections'])==len(packets)
    corrected=[s for s in result['trace'] if 'correction' in s]
    assert len(corrected)==len(packets)
    assert all(s['context']['kind']=='story' for s in corrected)
    assert all('actual_deltas' in s and 'numeric_choice' in s['correction'] for s in corrected)


def test_report_reads_failure_receipt_and_keeps_completed_offline_audit(tmp_path):
    def row(score,completed):
        return dict(seed='same',replicate=0,trace=[],ending={
            'quantitative_score':score,'completed':completed},final_state={
            'failure_reason':'' if completed else '绩效不佳'})
    save(tmp_path/'games/000.json',dict(baseline=row(25,False),candidate=row(65,True)))
    result=report(tmp_path)
    assert result['failure_reasons']=={'baseline':{'绩效不佳':1},'candidate':{'completed':1}}
    result.update(final=True,offline_audit='retained')
    save(tmp_path/'summary.json',result)
    assert report(tmp_path,final=True)['offline_audit']=='retained'
