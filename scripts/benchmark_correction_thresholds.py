#!/usr/bin/env python3
"""Paired routing ablation; reuse one answer pool without re-asking valid replies."""
import argparse
import asyncio
from collections import Counter, OrderedDict
import fcntl
import hashlib
import json
from itertools import combinations
import random
from pathlib import Path
import runpy
import shutil
import time

from dotenv import dotenv_values
from openai import AsyncOpenAI

from scripts.benchmark_bare_correction import AnswerCache, OUTPUT, canonical, read, save, usage_summary
from scripts.benchmark_symbolic_policy import ROOT, load_inputs, rollout
from scripts.compare_symbolic_scores import score_summary, paired_summary

ARMS = {'cutoff_060':dict(normal=.6,risk=.6),
        'cutoff_065':dict(normal=.65,risk=.65),
        'cutoff_070':dict(normal=.7,risk=.7),
        'extreme':dict(normal=.75,risk=.75),
        'cutoff_080':dict(normal=.8,risk=.8),
        'leader':dict(normal=.9,risk=.9)}
DIAGONAL_ARMS = dict(ARMS)
GRID_VALUES = (.6, .65, .7, .75, .8, .9)


def two_dimensional_arms():
    diagonal = {settings['normal']: name for name, settings in DIAGONAL_ARMS.items()}
    return {(diagonal[normal] if normal == risk else
             f'normal_{round(100*normal):03d}_risk_{round(100*risk):03d}'):
            dict(normal=normal, risk=risk)
            for normal in GRID_VALUES for risk in GRID_VALUES}


def memoized_decisions(function, stability=False, maxsize=50000):
    """Reuse pure decisions under one frozen policy; each result has its own dict."""
    cache, stats = OrderedDict(), Counter()
    def evaluate(*args, **kwargs):
        # Helpers/functions are fixed within this wrapper. Thresholds only
        # inspect support afterward; they are deliberately absent from this key.
        relevant = (*args[:5], kwargs.get('samples', 32)) if stability else args[:4]
        key = canonical(relevant)
        if key in cache:
            stats['hits'] += 1
            cache.move_to_end(key)
            result = cache[key]
        else:
            stats['misses'] += 1
            result = function(*args, **kwargs)
            cache[key] = result.copy() if isinstance(result, dict) else result
            if len(cache) > maxsize:
                cache.popitem(last=False)
        return result.copy() if isinstance(result, dict) else result
    evaluate.stats = stats
    return evaluate


def threshold_confidence(api,normal,risk):
    """Change only the cutoff; retain structural abstentions and 32/128 sampling."""
    policy=api['POLICY']
    helpers=api['_policy_helpers']()
    def assess(state,caps,options,context,selected):
        if context['kind']!='story' and selected is not None:
            return dict(confidence=1.,reason=None,samples=0)
        reason=api['semantic_deadlock'](state,caps,options,context)
        if reason or selected is None:
            return dict(confidence=0.,reason=reason or '符号策略无法推荐',samples=0)
        threshold=risk if state['R'] is None or state['R']>1 else normal
        evaluate=policy['ERRORS']['decision_stability']
        args=(state,caps,options,selected,context,helpers,policy['_choose_nominal'],policy['review'])
        result=evaluate(*args)
        if threshold-.125<=result['confidence']<=threshold+.125:
            initial=result['confidence']
            result=evaluate(*args,samples=128)
            result['initial_confidence']=initial
        result['reason']='决策置信度低于纠偏阈值' if result['confidence']<threshold else None
        return result
    return assess


def actions(rows):
    counts=Counter()
    failures=Counter()
    for row in rows:
        if '_actions' in row:
            cached = row['_actions']
            counts.update({k:v for k,v in cached.items() if isinstance(v,int)})
            failures.update(cached['failures'])
            continue
        failures['completed' if row['ending']['completed'] else row['final_state']['failure_reason']]+=1
        for s in row.get('trace',[]):
            if s['context']['kind']!='story':continue
            counts['ordinary_events']+=1
            c=s.get('correction')
            if not c:continue
            counts['corrections']+=1
            counts['structural_routes' if s['confidence']['samples']==0 else 'low_confidence_routes']+=1
            counts['changed']+=c['choice']!=c['numeric_choice']
            counts['leader_guard_violation']+=c['choice'] not in c['raw_guard_choices']
            counts['avoidable_leader_guard_violation']+=bool(c['raw_guard_choices']) and c['choice'] not in c['raw_guard_choices']
            for label in ('promotion_key_month','risk_recovery_conflict'):
                counts[label]+=bool(c[label])
            dr=s['actual_deltas']
            counts['numeric_actual_R_positive']+=dr[str(c['numeric_choice'])].get('R',0)>0
            counts['leader_actual_R_positive']+=dr[str(c['choice'])].get('R',0)>0
    return dict(counts,route_fraction=counts['corrections']/counts['ordinary_events'] if counts['ordinary_events'] else 0,
                failures=dict(failures))


def compact_row(row):
    return {k:row[k] for k in ('seed','replicate','ending')} | {'_actions':actions([row])}


def collect(root,reference,final=False,cache=None,snapshots=None):
    result=dict(final=final,arms={},comparisons={},cache_activity=dict(cache.stats if cache else {}))
    all_reference=([snapshots['original'][i] for i in sorted(snapshots['original'])] if snapshots is not None else
                   [compact_row(read(p)['candidate']) for p in sorted((reference/'games').glob('*.json'))])
    result['original']=dict(score_summary(all_reference),actions=actions(all_reference))
    by_index={i:r for i,r in enumerate(all_reference)}
    groups={}
    for name in ARMS:
        indexed=(snapshots.get(name,{}) if snapshots is not None else
                 {int(p.stem):compact_row(read(p)) for p in sorted((root/name/'games').glob('*.json'))})
        indices=sorted(indexed)
        rows=[indexed[i] for i in indices]
        if not rows:continue
        matched=[by_index[i] for i in indices]
        groups[name]=indexed
        result['arms'][name]=dict(score_summary(rows),actions=actions(rows),
            matched_original=score_summary(matched))
        if final:
            result['arms'][name]['paired_vs_original']=paired_summary(
                {(r['seed'],0):r for r in matched},{(r['seed'],0):r for r in rows})
            changes=[b['ending']['quantitative_score']-a['ending']['quantitative_score'] for a,b in zip(matched,rows)]
            rng=random.Random(20261005)
            draws=sorted(sum(rng.choice(changes) for _ in changes)/len(changes) for _ in range(4000))
            tail=.025/len(ARMS)
            result['arms'][name]['mean_change_ci_familywise95']=[draws[int(tail*len(draws))],draws[int((1-tail)*len(draws))]]
    grid_2d=any(a['normal']!=a['risk'] for a in ARMS.values())
    pairs=([( 'cutoff_070', name) for name in groups if name!='cutoff_070']+
           [('extreme','leader')] if grid_2d else list(combinations(groups,2))) if final else []
    for before,after in pairs:
        shared=groups[before].keys() & groups[after].keys()
        if shared:
            result['comparisons'][f'{after}_vs_{before}']=paired_summary(
                {(groups[before][i]['seed'],0):groups[before][i] for i in shared},
                {(groups[after][i]['seed'],0):groups[after][i] for i in shared})
    save(root/'summary.json',result)
    if final:
        result['pool_usage_cumulative']=usage_summary(reference/'events')
        save(root/'summary.json',result)
        lines=['# 纠偏门槛：128 种子配对验证','',
            '同一冻结符号策略、误差模型和种子；同一 .env 提供商 deepseek-v4.1-flash；共用原答案池。',
            '原决策包与提示不变，缓存中每个有效答案原样复用，未命中才补调用。',
            '支持率低于阈值才纠偏，因此提高支持率阈值等于降低纠偏的不稳定度门槛。结构性纠偏与所有固定红线不变。','',
            '|组别|支持率阈值（常规/R偏高）|中位数 /100|均分|48月完成|纠偏占比|',
            '|---|---|---:|---:|---:|---:|']
        settings=[('原规则','original','0.4375 / 0.50')]+[
            (f"{a['normal']:.2g}/{a['risk']:.2g} 档",name,f"{a['normal']:.4g} / {a['risk']:.4g}")
            for name,a in reversed(list(ARMS.items()))]
        for label,key,cutoff in settings:
            s=result['original'] if key=='original' else result['arms'][key]
            lines.append(f"|{label}|{cutoff}|{s['median']:.2f}|{s['mean']:.2f}|{s['completed']}/{s['n']}|{100*s['actions']['route_fraction']:.2f}%|")
        if grid_2d:
            for metric,title in [('median','量化中位数'),('mean','量化均分')]:
                lines+=['',f'## {title}：行=常规门槛，列=R>1或R未知门槛','',
                        '|常规 / R|'+'|'.join(f'{x:g}' for x in reversed(GRID_VALUES))+'|',
                        '|---|'+'---:|'*len(GRID_VALUES)]
                for normal in reversed(GRID_VALUES):
                    cells=[]
                    for risk in reversed(GRID_VALUES):
                        name=next(n for n,a in ARMS.items() if a==dict(normal=normal,risk=risk))
                        cells.append(f"{result['arms'][name][metric]:.3f}")
                    lines.append(f'|{normal:g}|'+'|'.join(cells)+'|')
        lines+=['','当前 R 真值、真实封顶已知、普通 D 未翻译、独立误差边际、单轮裸事件上下文等假设沿用上轮。',
            '仅比较用户指定的已测门槛；不能据有限门槛证明纠偏数量越多越好。',
            '各组中位数区间及配对差区间均为点态区间；各组相对原组的主要均分比较另报告多重比较调整后的区间。','',
            '```json',json.dumps(result,ensure_ascii=False,indent=2),'```']
        (root/'report.md').write_text('\n'.join(lines)+'\n')
    return result


async def run(args):
    lock=(OUTPUT/'.benchmark.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    reference=OUTPUT/args.reference
    previous=read(reference/'manifest.json')
    if previous['seeds']!=128 or len(list((reference/'games').glob('*.json')))!=128:
        raise ValueError('The frozen 128-game reference must be complete')
    env=dotenv_values(ROOT/'.env')
    if env.get('MODEL_NAME')!=previous['model'] or hashlib.sha256((env.get('API_BASE') or '').encode()).hexdigest()!=previous['api']['base_sha256']:
        raise ValueError('Approved .env provider/model changed')
    root=OUTPUT/args.run
    if not (root/'manifest.json').exists():
        root.mkdir(parents=True,exist_ok=True)
        for name in ('policy','error-model.json','conditional.py'):
            src=reference/name;target=root/name
            if src.is_dir():shutil.copytree(src,target)
            else:shutil.copyfile(src,target)
        shutil.copyfile(Path(__file__),root/'benchmark_correction_thresholds.py')
        shutil.copyfile(ROOT/'scripts/benchmark_symbolic_policy.py',root/'benchmark_symbolic_policy.py')
        shutil.copyfile(ROOT/'scripts/benchmark_bare_correction.py',root/'benchmark_bare_correction.py')
        initial_keys=sorted(p.stem for p in (reference/'events').glob('*.json'))
        save(root/'initial-cache-keys.json',initial_keys)
        save(root/'manifest.json',dict(reference=args.reference,seeds=128,cohort=previous['cohort'],
            model=previous['model'],system=previous['system'],arms=ARMS,
            threshold_definition='normal for R<=1; risk for R>1 or R unknown; support<cutoff routes to Leader',
            selection_rule='Maximum full-cohort quantitative median; ties: completion count, mean, fewer corrections.',
            answer_pool=str(reference/'events'),initial_keys=len(initial_keys),
            assumptions=previous['assumptions'],source_hashes={str(p.relative_to(root)):hashlib.sha256(p.read_bytes()).hexdigest()
                for p in root.rglob('*') if p.is_file()}))
    manifest=read(root/'manifest.json')
    if manifest['arms']!=ARMS or manifest['reference']!=args.reference:raise ValueError('Sweep plan changed')
    for name,digest in manifest['source_hashes'].items():
        if hashlib.sha256((root/name).read_bytes()).hexdigest()!=digest:raise ValueError('Frozen sweep source changed')
    for name in ('benchmark_correction_thresholds.py','benchmark_symbolic_policy.py','benchmark_bare_correction.py'):
        if hashlib.sha256((ROOT/'scripts'/name).read_bytes()).hexdigest()!=manifest['source_hashes'][name]:
            raise ValueError('Live benchmark controller changed; frozen run cannot resume')
    if args.reuse_sweep:
        older=OUTPUT/args.reuse_sweep
        old=read(older/'manifest.json')
        if old['reference']!=args.reference:raise ValueError('Previous sweep reference differs')
        if any(old[k]!=manifest[k] for k in ('cohort','model','system')):
            raise ValueError('Previous cohort or Leader request differs')
        for name in ('policy/numeric-policy.py','policy/read-context.py','policy/translation-error.py',
                     'policy/translation-errors.json','error-model.json','conditional.py',
                     'benchmark_symbolic_policy.py','benchmark_bare_correction.py'):
            if hashlib.sha256((older/name).read_bytes()).hexdigest()!=manifest['source_hashes'][name]:
                raise ValueError(f'Previous frozen source differs: {name}')
        for name,settings in ARMS.items():
            previous_settings=old['arms'].get(name,{})
            if any(previous_settings.get(k)!=v for k,v in settings.items()) or previous_settings.get('cap_note',False):continue
            for path in (older/name/'games').glob('*.json'):
                target=root/name/'games'/path.name
                if not target.exists():save(target,read(path))
    paths=(root/'policy/numeric-policy.py',root/'policy/read-context.py',root/'error-model.json',root/'conditional.py')
    policy,base_helpers,errors=load_inputs(*paths)
    api=runpy.run_path(str(paths[1]))
    memoized=[]
    if args.two_dimensional:
        for name in ('choose','_choose_nominal'):
            policy[name]=memoized_decisions(policy[name]);memoized.append(policy[name])
        stability=api['POLICY']['ERRORS']['decision_stability']
        api['POLICY']['ERRORS']['decision_stability']=memoized_decisions(stability,stability=True)
        memoized.append(api['POLICY']['ERRORS']['decision_stability'])
    helpers={name:dict(base_helpers,confidence=threshold_confidence(api,a['normal'],a['risk'])) for name,a in ARMS.items()}
    snapshots={'original':{int(p.stem):compact_row(read(p)['candidate']) for p in sorted((reference/'games').glob('*.json'))}}
    snapshots.update({name:{int(p.stem):compact_row(read(p)) for p in (root/name/'games').glob('*.json')} for name in ARMS})
    queue=asyncio.Queue()
    pending={name:[i for i in range(128) if not (root/name/'games'/f'{i:03d}.json').exists()] for name in ARMS}
    for offset in range(max(map(len,pending.values()))):
        for name,indices in pending.items():
            if offset<len(indices):queue.put_nowait((name,indices[offset]))
    start=time.monotonic()
    async with AsyncOpenAI(api_key=env['API_KEY'],base_url=env['API_BASE'],timeout=90,max_retries=0) as client:
        cache=AnswerCache(reference/'events',client,manifest['system'],model=manifest['model'])
        async def worker():
            resources={}
            while not queue.empty():
                name,i=queue.get_nowait()
                async def correct(packet,state_context):
                    return await cache(packet,state_context)
                seed=f"error-policy-20261005-{manifest['cohort']}-{i:03d}"
                row=await rollout(policy,helpers[name],errors,seed,0,'calibrated',True,True,
                    resources=resources,correction=correct)
                save(root/name/'games'/f'{i:03d}.json',row)
                snapshots[name][i]=compact_row(row)
                count=len(snapshots[name])
                print(json.dumps(dict(stage='games',arm=name,index=i,completed=count,score=row['ending']['quantitative_score'],
                    routes=len(row['corrections']),elapsed=round(time.monotonic()-start,1))),flush=True)
                if count%16==0:collect(root,reference,cache=cache,snapshots=snapshots)
        tasks=[asyncio.create_task(worker()) for _ in range(args.workers)]
        try:await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:task.cancel()
            await asyncio.gather(*tasks,return_exceptions=True)
            collect(root,reference,cache=cache,snapshots=snapshots)
            raise
        result=collect(root,reference,final=True,cache=cache,snapshots=snapshots)
        if memoized:
            result['deterministic_compute_cache']=[dict(f.stats) for f in memoized]
            save(root/'summary.json',result)
        print(json.dumps(result,ensure_ascii=False),flush=True)


def main():
    global ARMS
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--reference',default='20261005-bare-leader-128-v3')
    parser.add_argument('--run')
    parser.add_argument('--two-dimensional',action='store_true')
    parser.add_argument('--reuse-sweep')
    parser.add_argument('--workers',type=int,default=4)
    args=parser.parse_args()
    if args.two_dimensional:ARMS=two_dimensional_arms()
    args.run=args.run or ('20261006-routing-threshold-grid-2d-128-v1' if args.two_dimensional else
                          '20261005-routing-threshold-grid-128-v1')
    if not 1<=args.workers<=24:parser.error('workers must be 1..24')
    asyncio.run(run(args))


if __name__=='__main__':main()
