#!/usr/bin/env python3
"""Isolated semantic correction with one durable answer per exact public context.

The official emulator uses MemoryStore. No Jiuwen workspace, session or database
is opened or changed. Only public bare-event packets reach the configured API.
"""
import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import shutil
import time

from dotenv import dotenv_values
from openai import AsyncOpenAI

from career_sim_runner.constants import JIUWEN_PLAYER_MODEL
from career_sim_runner.paths import jiuwenswarm_env_path
from scripts.benchmark_symbolic_policy import ROOT, load_inputs, rollout
from scripts.compare_symbolic_scores import score_summary, paired_summary

VERSION = 'bare-correction-v3'
BENCHMARK_MODEL = 'deepseek-v4.1-flash'  # Explicit user override for this benchmark only.
SOURCES = ROOT/'solution/skills/observe-decide-review'
OUTPUT = ROOT/'.career_sim_runner/bare_correction_benchmark'


def canonical(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'))


def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(value,ensure_ascii=False,indent=2)+'\n')
    tmp.replace(path)


def read(path):
    return json.loads(path.read_text())


def request_for(packet,system,model=JIUWEN_PLAYER_MODEL):
    if set(packet)-{'current_event','event_history','options','constraints'}:
        raise ValueError('Only public bare-event packet is accepted')
    if set(packet['constraints'])!={'守红线','晋升条件'}:
        raise ValueError('Unexpected correction constraints')
    for option in packet['options']:
        if set(option)-{'choice','action','description'}:
            raise ValueError('Private menu metadata in correction packet')
    return dict(model=model,
        messages=[{'role':'system','content':system},
                  {'role':'user','content':canonical(packet)}],
        tools=[{'type':'function','function':{'name':'take_action',
            'description':'提交一个原始选项编号及一句简短行为理由。',
            'parameters':{'type':'object','properties':{
                'choice':{'type':'integer','enum':[o['choice'] for o in packet['options']]},
                'notes':{'type':'string'}},'required':['choice','notes'],'additionalProperties':False}}}],
        tool_choice={'type':'function','function':{'name':'take_action'}},
        temperature=.95,top_p=.1,max_tokens=256,
        extra_body={'thinking':{'type':'disabled'}})


class PilotDone(Exception):
    pass


class AnswerCache:
    def __init__(self,directory,client,system,pilot=False,model=JIUWEN_PLAYER_MODEL):
        self.directory,self.client,self.system=directory,client,system
        self.model=model
        self.pilot=pilot
        self.locks={}
        self.stats=Counter()

    async def __call__(self,packet,public_cache_context):
        request=request_for(packet,self.system,self.model)
        # The public state/time distinguishes cache contexts but is NOT an
        # extra model input: the formal Leader handoff supplies constraints.
        identity=dict(version=VERSION,request=request,public_cache_context=public_cache_context)
        key=hashlib.sha256(canonical(identity).encode()).hexdigest()
        path=self.directory/(key+'.json')
        async with self.locks.setdefault(key,asyncio.Lock()):
            record=read(path) if path.exists() else dict(identity,cache_key=key,attempts=[])
            if record.get('accepted'):
                self.stats['cache_hits']+=1
                return dict(record['answer'],cache_key=key)
            if self.pilot and self.stats['accepted']:
                raise PilotDone()
            if record.get('in_flight'):
                pending=record.pop('in_flight')
                record['attempts'].append(dict(pending,error='InterruptedUnknownUsage',usage=None))
                save(path,record)
            for attempt in range(len(record['attempts']),2):
                started=time.monotonic()
                item=dict(attempt=attempt,started_utc=datetime.now(timezone.utc).isoformat(),usage=None)
                record['in_flight']=item
                save(path,record)
                self.stats['calls']+=1
                try:
                    response=await self.client.chat.completions.create(**request)
                    item['response']=response.choices[0].message.model_dump(exclude_none=True)
                    if response.usage is not None:
                        item['usage']=response.usage.model_dump(exclude_none=True)
                    calls=response.choices[0].message.tool_calls
                    if not calls or len(calls)!=1 or calls[0].function.name!='take_action':
                        raise ValueError('Expected one take_action tool call')
                    answer=json.loads(calls[0].function.arguments)
                    if set(answer)!={'choice','notes'} or type(answer['choice']) is not int or answer['choice'] not in {
                            o['choice'] for o in packet['options']} or not isinstance(answer['notes'],str) or not answer['notes'].strip():
                        raise ValueError('Unusable action answer')
                    record.update(accepted=True,answer=answer)
                    item['accepted']=True
                    self.stats['accepted']+=1
                except asyncio.CancelledError:
                    # Leave the durable in-flight marker for honest accounting.
                    raise
                except Exception as exc:
                    item['error']=type(exc).__name__
                    if getattr(exc,'status_code',None) is not None:
                        item['status_code']=exc.status_code
                item['seconds']=round(time.monotonic()-started,3)
                record['attempts'].append(item)
                record.pop('in_flight',None)
                save(path,record)
                if item.get('accepted'):
                    n=self.stats['accepted']
                    if n==1 or n%16==0:
                        print(json.dumps(dict(stage='answers',**self.stats),ensure_ascii=False),flush=True)
                    return dict(record['answer'],cache_key=key)
            raise RuntimeError(f'Two unusable API attempts; inspect cache {key}')


def usage_summary(directory):
    stats=Counter()
    for path in directory.glob('*.json'):
        record=read(path)
        stats['files']+=1
        stats['accepted_contexts']+=bool(record.get('accepted'))
        attempts=[*record['attempts'],*([record['in_flight']] if record.get('in_flight') else [])]
        for item in attempts:
            stats['attempts']+=1
            usage=item.get('usage')
            if usage is None:
                stats['unknown_usage_attempts']+=1
                continue
            for name in ('prompt_tokens','completion_tokens','total_tokens'):
                if usage.get(name) is not None: stats[name]+=usage[name]
            reason=(usage.get('completion_tokens_details') or {}).get('reasoning_tokens')
            if reason is not None: stats['reasoning_tokens']+=reason
    return dict(stats)


def report(output,cache_stats=None,final=False):
    pairs=[read(p) for p in sorted((output/'games').glob('*.json'))]
    # Retain the completed offline audit when replaying an already finished run.
    if final and (output/'summary.json').exists():
        prior=read(output/'summary.json')
        if prior.get('final') and prior['completed_pairs']==len(pairs):return prior
    result=dict(completed_pairs=len(pairs),final=final,api_usage=usage_summary(output/'events'),
                cache_activity=dict(cache_stats or {}))
    if pairs:
        a,b=[p['baseline'] for p in pairs],[p['candidate'] for p in pairs]
        result.update(baseline=score_summary(a),candidate=score_summary(b),
            paired=paired_summary({(r['seed'],0):r for r in a},{(r['seed'],0):r for r in b}))
        groups={'all':[], 'promotion_key_month':[], 'risk_recovery_conflict':[]}
        for row in b:
            for step in row['trace']:
                if 'correction' not in step:continue
                c=step['correction']
                groups['all'].append(step)
                for name in groups:
                    if name!='all' and c[name]:groups[name].append(step)
        result['correction_actions']={}
        for name,steps in groups.items():
            counts=Counter(events=len(steps))
            for s in steps:
                c=s['correction'];ids=c['raw_guard_choices']
                counts['changed']+=c['choice']!=c['numeric_choice']
                counts['numeric_guard_violation']+=c['numeric_choice'] not in ids
                counts['leader_guard_violation']+=c['choice'] not in ids
                if c['oracle_choice'] is not None:
                    counts['oracle_available']+=1
                    # Same one-step fixed rule on true deltas: diagnostic only.
                    counts['numeric_oracle_matches']+=c['numeric_choice']==c['oracle_choice']
                    counts['leader_oracle_matches']+=c['choice']==c['oracle_choice']
                delta=s['actual_deltas']
                r0=delta[str(c['numeric_choice'])].get('R',0)
                r1=delta[str(c['choice'])].get('R',0)
                counts['numeric_actual_R_positive']+=r0>0
                counts['leader_actual_R_positive']+=r1>0
                counts['R_reduced_vs_numeric']+=r1<r0
                counts['R_increased_vs_numeric']+=r1>r0
            result['correction_actions'][name]=dict(counts)
        result['failure_reasons']={label:dict(Counter('completed' if r['ending']['completed'] else r['final_state']['failure_reason'] for r in rows))
                                   for label,rows in (('baseline',a),('candidate',b))}
    save(output/'summary.json',result)
    if final:
        lines=['# Leader 裸事件纠偏：128 种子最小验证','',
            '独立 deepseek-v4.1-flash API（用户明确允许）；仓库 .env 提供商；正式纠偏包；六指标方向/幅度条件误差；固定事件精确。',
            '当前 R 真值、真实封顶已知，普通事件 D 未翻译；这些沿用旧离线估分假设。',
            '每次纠偏均接管。原题/前情/选项/当前约束之外，预测、路由原因、真增量与终局分数均不入请求。',
            '每个完整上下文的有效答案只取一次，错误答案也复用。状态与月份纳入缓存键。',
            '正式 Leader 长会话与本测试独立单轮上下文有差异；新事件池和 R 状态估计误差未覆盖。','',
            '|策略|量化中位数 /100|均分|活满 48 月|','|---|---:|---:|---:|']
        for label,key in [('纯脚本','baseline'),('裸事件纠偏','candidate')]:
            s=result[key];lines.append(f"|{label}|{s['median']:.2f}|{s['mean']:.2f}|{s['completed']}/{s['n']}|")
        lines+=['','配对变化、置信区间、各类纠偏证据：','```json',
                json.dumps({k:v for k,v in result.items() if k not in ('baseline','candidate')},ensure_ascii=False,indent=2),
                '```','', '真实增量仅存于 games 中用于离线评估，events 中的模型请求没有隐藏事件答案。']
        (output/'report.md').write_text('\n'.join(lines)+'\n')
    return result


async def run(args):
    OUTPUT.mkdir(parents=True,exist_ok=True)
    lock=(OUTPUT/'.benchmark.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    env_path=ROOT/'.env'
    env=dotenv_values(env_path)
    if env.get('MODEL_NAME')!=BENCHMARK_MODEL:
        raise ValueError('Repository .env must serve the explicitly approved deepseek-v4.1-flash model')
    api_identity=dict(env_file=str(env_path),base_sha256=hashlib.sha256(
        (env.get('API_BASE') or '').encode()).hexdigest())
    output=OUTPUT/args.run
    names=['numeric-policy.py','read-context.py','refresh-context.py','translation-error.py','translation-errors.json']
    if not (output/'manifest.json').exists():
        output.mkdir(parents=True,exist_ok=True)
        for name in names:
            target=output/'policy'/name;target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(SOURCES/'scripts'/name,target)
        shutil.copyfile(SOURCES/'SKILL.md',output/'leader-SKILL.md')
        shutil.copyfile(ROOT/'logs/conditional_model-coherent-20261005.json',output/'error-model.json')
        shutil.copyfile(ROOT/'logs/conditional_current.py',output/'conditional.py')
        for name in ('benchmark_bare_correction.py','benchmark_symbolic_policy.py'):
            shutil.copyfile(ROOT/'scripts'/name,output/name)
        skill=(output/'leader-SKILL.md').read_text()
        paragraph=skill.split('2. 否则说明推荐可信度过低',1)[1].split('季度体力行动',1)[0].strip()
        system='你是 CareerSim Leader。当前是自主纠偏决策轮。'+paragraph+'\n调用 take_action 提交一个原始选项编号和一句简短行为理由。'
        save(output/'manifest.json',dict(version=VERSION,seeds=args.seeds,cohort=args.cohort,api=api_identity,
            source_hashes={str(p.relative_to(output)):hashlib.sha256(p.read_bytes()).hexdigest()
                for p in output.rglob('*') if p.is_file()},system=system,model=BENCHMARK_MODEL,
            mode='all existing correction routes; fixed numeric choices elsewhere',
            assumptions=['gold current R','true caps','ordinary D omitted','independent error marginals',
                         'fresh single-turn Leader context, formal bare packet'],
            cache='exact request + public state/time; one accepted answer including mistakes'))
    manifest=read(output/'manifest.json')
    if manifest['api']!=api_identity:raise ValueError('Resume API route changed')
    if (manifest['version'],manifest['seeds'],manifest['cohort'])!=(VERSION,args.seeds,args.cohort):
        raise ValueError('Resume configuration changed')
    for name,digest in manifest['source_hashes'].items():
        if hashlib.sha256((output/name).read_bytes()).hexdigest()!=digest:raise ValueError('Frozen source changed')
    for name in ('benchmark_bare_correction.py','benchmark_symbolic_policy.py'):
        if hashlib.sha256((ROOT/'scripts'/name).read_bytes()).hexdigest()!=manifest['source_hashes'][name]:
            raise ValueError('Harness changed; run frozen source instead')
    if args.reuse_baseline_run:
        previous=OUTPUT/args.reuse_baseline_run
        old=read(previous/'manifest.json')
        if (old['cohort'],old['seeds'])!=(manifest['cohort'],manifest['seeds']):
            raise ValueError('Baseline cohort mismatch')
        for name in [*('policy/'+n for n in names),'error-model.json','conditional.py']:
            if old['source_hashes'][name]!=manifest['source_hashes'][name]:
                raise ValueError('Baseline frozen inputs differ')
        for path in (previous/'baselines').glob('*.json'):
            target=output/'baselines'/path.name
            if not target.exists():save(target,read(path))
        save(output/'baseline-reuse.json',dict(source_run=args.reuse_baseline_run,
            source_manifest_sha256=hashlib.sha256((previous/'manifest.json').read_bytes()).hexdigest(),
            copied=len(list((output/'baselines').glob('*.json')))))
    inputs=load_inputs(output/'policy/numeric-policy.py',output/'policy/read-context.py',
                       output/'error-model.json',output/'conditional.py')
    queue=asyncio.Queue()
    for i in range(args.seeds):
        if not (output/'games'/f'{i:03d}.json').exists():queue.put_nowait(i)
    started=time.monotonic()
    async with AsyncOpenAI(api_key=env['API_KEY'],base_url=env['API_BASE'],timeout=90,max_retries=0) as client:
        cache=AnswerCache(output/'events',client,manifest['system'],args.pilot,manifest['model'])
        async def worker():
            # A resolver cache is isolated per lane; its seeded wrapper never
            # crosses games that can interleave on API awaits.
            resources={}
            while not queue.empty():
                i=queue.get_nowait()
                seed=f'error-policy-20261005-{args.cohort}-{i:03d}'
                baseline_path=output/'baselines'/f'{i:03d}.json'
                if baseline_path.exists():baseline=read(baseline_path)
                else:
                    baseline=await rollout(*inputs,seed,0,'calibrated',False,False,resources=resources)
                    save(baseline_path,baseline)
                candidate=await rollout(*inputs,seed,0,'calibrated',True,True,resources=resources,correction=cache)
                save(output/'games'/f'{i:03d}.json',dict(index=i,baseline=baseline,candidate=candidate))
                n=len(list((output/'games').glob('*.json')))
                print(json.dumps(dict(stage='games',completed=n,target=args.seeds,index=i,
                    baseline=baseline['ending']['quantitative_score'],candidate=candidate['ending']['quantitative_score'],
                    corrections=len(candidate['corrections']),elapsed=round(time.monotonic()-started,1))),flush=True)
                if n%16==0:report(output,cache.stats)
        tasks=[asyncio.create_task(worker()) for _ in range(1 if args.pilot else args.workers)]
        try:await asyncio.gather(*tasks)
        except PilotDone:
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            report(output,cache.stats)
            print(json.dumps(dict(stage='pilot_saved',path=str(output),**cache.stats)),flush=True)
        except Exception:
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            report(output,cache.stats)
            raise
        else:
            result=report(output,cache.stats,final=True)
            print(json.dumps(result,ensure_ascii=False),flush=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run',default='20261005-bare-leader-128-v3')
    parser.add_argument('--seeds',type=int,default=128)
    parser.add_argument('--cohort',default='bare-leader-holdout-v1')
    parser.add_argument('--workers',type=int,default=4)
    parser.add_argument('--pilot',action='store_true')
    parser.add_argument('--reuse-baseline-run')
    args=parser.parse_args()
    if args.seeds<1 or not 1<=args.workers<=4:parser.error('Positive seeds and workers 1..4 required')
    asyncio.run(run(args))


if __name__=='__main__':main()
