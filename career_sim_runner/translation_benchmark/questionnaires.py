"""Isolated parallel questionnaire agents; only deterministic code sees hidden GT."""
from __future__ import annotations
import asyncio
from collections import Counter
from dataclasses import replace
from datetime import datetime, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import runpy
import shutil
import sqlite3
import time

from dotenv import dotenv_values
from openai import AsyncOpenAI
from career_sim_runner.constants import REPO_ROOT, JIUWEN_PLAYER_MODEL
from career_sim_runner.paths import jiuwenswarm_env_path
from career_sim_runner.setup import configured_model_name
from .cases import load_cases
from .scoring import direction_cosine, effect_value, classification_errors, directional_error_summary

GROUPS = ('O', 'N', 'S', 'HW', 'R')
METRICS = 'ONS HWR'.replace(' ', '')
OUTPUT_ROOT = REPO_ROOT / '.career_sim_runner/translation_benchmark'
VERSION = 'parallel-questionnaires-v4'


def read(path):
    return json.loads(path.read_text())


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    tmp.replace(path)


def rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def append(path, value):
    with path.open('a') as stream:
        stream.write(json.dumps(value, ensure_ascii=False) + '\n')
        stream.flush()


def usage_summary(attempts):
    result = {k: sum((a.get('usage') or {}).get(k, 0) or 0 for a in attempts)
              for k in ('input_tokens', 'output_tokens', 'total_tokens', 'reasoning_tokens', 'cached_tokens')}
    automatic = sum(a.get('attempt', 0) > 0 for a in attempts)
    retried = len(attempts) - len({a['cursor'] for a in attempts}) if all('cursor' in a for a in attempts) else 0
    result.update(calls=len(attempts), retries=retried, automatic_retries=automatic,
                  targeted_retries=max(0,retried-automatic),
                  failed_calls=sum('error' in a for a in attempts),
                  unavailable_usage_calls=sum(a.get('usage') is None for a in attempts),
                  unavailable_reasoning_calls=sum((a.get('usage') or {}).get('reasoning_tokens') is None for a in attempts),
                  model_seconds=sum(a.get('seconds', 0) for a in attempts))
    return result


def scores(cases, predictions):
    cosine = []; exact = []; full = []; fp = fn = omissions = reversals = 0
    per = {m: Counter() for m in METRICS}; failing = []
    for case in cases:
        pred = predictions[case.case_id]
        wrong = []
        for choice, gt in case.expected.items():
            pp = pred.get(str(choice), {})
            cosine.append(direction_cosine(gt, pp))
            matches = []
            for m in METRICS:
                g, p = effect_value(m, gt.get(m)), effect_value(m, pp.get(m))
                ok = abs(g-p) <= .5
                matches.append(ok); exact.append(ok)
                per[m].update(cells=1, exact=int(ok), omissions=int(g != 0 and p == 0),
                              reversals=int(g*p < 0), zero_fp=int(g == 0 and p != 0))
                per[m]['cosine_sum'] += direction_cosine({m: gt.get(m)}, {m: pp.get(m)})
            full.append(all(matches))
            if not all(matches): wrong.append(choice)
        # Drop D from both sides for fair six-metric accuracy and FP/FN.
        expected = {k: {m:v for m,v in row.items() if m != 'D'} for k,row in case.expected.items()}
        predicted = {int(k): {m:v for m,v in row.items() if m != 'D'} for k,row in pred.items()}
        a,b = classification_errors(expected, predicted)
        fp += sum(map(len,a.values())); fn += sum(map(len,b.values()))
        d = directional_error_summary(expected, predicted)
        omissions += d['omission_count']; reversals += d['reversal_count']
        if wrong: failing.append({'case_id':case.case_id,'title':case.shorttitle,'wrong_options':wrong})
    return dict(events=len(cases), options=len(cosine), cosine_pct=100*sum(cosine)/len(cosine),
                metric_accuracy_pct=100*sum(exact)/len(exact), option_accuracy_pct=100*sum(full)/len(full),
                fp=fp, fn=fn, omissions=omissions, reversals=reversals, per_metric=per, failing=failing)


def baseline(cases, ledger, run_id=None):
    """Choose best completed full-pool run, never best on the tested subset."""
    with sqlite3.connect(ledger) as db:
        db.row_factory = sqlite3.Row
        if run_id:
            run = db.execute('SELECT * FROM runs WHERE run_id=? AND completed_at IS NOT NULL', (run_id,)).fetchone()
        else:
            run = db.execute("SELECT * FROM runs WHERE mode='dev-full' AND completed_at IS NOT NULL ORDER BY direction_score DESC LIMIT 1").fetchone()
        if run is None: raise ValueError('No completed dictionary baseline')
        saved = {r['case_id']:dict(r) for r in db.execute('SELECT * FROM results WHERE run_id=?',(run['run_id'],))}
        selected = [saved[c.case_id] for c in cases]
    for c,r in zip(cases,selected):
        if json.loads(r['expected_json']) != {str(k):v for k,v in c.expected.items()}:
            raise ValueError('Baseline GT differs from current pool')
    predictions = {r['case_id']:json.loads(r['actual_json']) for r in selected}
    usage = [dict(usage=json.loads(r['usage_json'])) for r in selected]
    models=sorted({m for r in selected for m in json.loads(r['usage_json']).get('by_model',{})})
    return dict(run=dict(run), recorded_models=models, scores=scores(cases,predictions), predictions=predictions,
                usage=usage_summary(usage),
                usage_note='Historical result-row usage may cover batches/reviews, not these cases alone; see original run. No fresh baseline calls.',
                original_rows=selected)


def question_cursor(state, notes, group):
    """Keep benchmark replay bookkeeping outside the live questionnaire."""
    task = state['current_task'](notes)
    lane = state['event_dir'](notes, task) / group
    if (lane / 'complete.json').exists():
        return None
    path = lane / '.answers.json'
    answers = read(path) if path.exists() else {}
    return state['digest']([task['observation_hash'], group, answers])[:16]


def submission_tool(state, notes, group):
    """Describe only the current script's accepted structure; add no judgment rules."""
    lane = state['event_dir'](notes, state['current_task'](notes)) / group
    event = read(lane / '.event.json')
    values = read(lane / '.answers.json') if (lane / '.answers.json').exists() else {}
    engine = runpy.run_path(str(Path(state['ROOT']) / 'scripts/questionnaires' / f'{group}.py'))
    ids = [str(c['choice']) for c in event['choices']]

    def object_schema(properties):
        return {'type': 'object', 'properties': properties, 'required': list(properties),
                'additionalProperties': False}

    integer = {'type': 'integer', 'enum': list(range(-3, 4))}
    if group == 'O':
        _, key = engine['current'](event, values)
        pending = engine['pending'](event, values)[0] if 'pending' in engine else []
        if len(pending) > 1:
            answer = object_schema({str(c['choice']): {'type': 'integer', 'enum': sorted(engine['ALLOWED'][key])}
                                    for c in pending})
        else:
            answer = {'type': 'string', 'enum': [str(v) for v in sorted(engine['ALLOWED'][key])]}
    elif group == 'N' and 'current' in engine:
        phase = engine['current'](values)
        if phase == 'route':
            answer = object_schema({'joint': {'type': 'boolean'}})
        else:
            allowed = [-1, 0] if phase == 'participation' else list(range(-3, 4))
            item = object_schema({'N': {'type': 'integer', 'enum': allowed}})
            answer = object_schema({k: item for k in ids})
    elif group == 'N' and 'SCENES' in engine and '_scene' not in values:
        answer = object_schema({'scene': {'type': 'string', 'enum': list(engine['SCENES'])}})
    elif group == 'HW' and '_route' not in values:
        answer = object_schema({k: {'type': 'boolean'} for k in ('existing_H_load', 'physical_risk')})
    elif group == 'HW' and '_joint' in values:
        answer = object_schema({k: {'type': 'boolean'} for k in ids})
    elif group == 'R':
        phase, choices = engine['current'](event, values, read(lane / '.policy.json'))
        allowed = engine.get('ALLOWED', {1: [0, 2, 3], 2: [-2, -1, 0], 3: list(range(-2, 4))})[phase]
        answer = object_schema({str(c['choice']): {'type': 'integer', 'enum':
            engine['allowed'](phase, values, str(c['choice'])) if 'allowed' in engine else allowed}
            for c in choices})
    else:
        if group == 'N' and 'SCENES' in engine:
            allowed = [-1, 0, 1] if values['_scene'] == engine['SCENES'][0] else list(range(-3, 4))
            fields = {'N': {'type': 'integer', 'enum': allowed}}
            # Older frozen candidates keep their original output contract on resume.
            if 'reason' in read(lane / '.policy.json')['questions']['输出']:
                fields['reason'] = {'type': 'string', 'minLength': 1, 'maxLength': 60}
            item = object_schema(fields)
        else:
            item = object_schema({m: integer for m in group})
        answer = object_schema({k: item for k in ids})
    return {'type': 'function', 'function': {'name': 'submit',
            'description': '按当前题提交全部所需答案，返回下一题或完成。',
            'parameters': object_schema({'answer': answer})}}


async def translate_group(client, model, state, notes, group, directory):
    """A separate conversation and persisted lane per metric group; no GT argument."""
    lane = directory / group
    lane.mkdir(parents=True,exist_ok=True)
    history_path = lane / 'conversation.json'
    attempts_path = lane / 'attempts.jsonl'
    inflight = lane / 'in-flight.json'
    if inflight.exists():
        pending = read(inflight)
        if any(a.get('started')==pending.get('started') for a in rows(attempts_path)):
            inflight.unlink()  # Response was durably accounted before interruption.
    # The same role instructions, with only the tool transport substituted.
    role = (Path(state['ROOT']) / 'stages' / f'analyse_{group}.md').read_text()
    system = ('你是事件指标问卷答题者。脚本已调用 ready 装载当前题。用 submit 的 answer 参数提交当前答案；'
              '参数类型与格式以工具 schema 和当前题为准。题目完成即结束。只回答本题，不解释、不复核、不读其他指标。')
    system += '\n\n' + role[role.index('## '):]
    packet = (state['question'](group, notes) if 'question' in state
              else state['answer'](group, 'ready', notes))
    if packet['complete']: return
    if history_path.exists():
        saved = read(history_path)
        messages = saved['messages'] if isinstance(saved,dict) else saved
        current_cursor = question_cursor(state,notes,group)
        if isinstance(saved,dict) and saved['cursor'] != current_cursor:
            previous = next(a for a in rows(attempts_path) if a['cursor']==saved['cursor'] and a.get('accepted'))
            messages += [previous['response'],{'role':'tool','tool_call_id':previous['response']['tool_calls'][0]['id'],
                         'content':json.dumps({k:v for k,v in packet.items() if k!='event'},ensure_ascii=False)}]
    else:
        messages = [{'role':'system','content':system},
                    {'role':'user','content':'请调用脚本回答新问题'},
                    {'role':'user','content':json.dumps(packet,ensure_ascii=False)}]
        save(history_path,{'messages':messages,'cursor':question_cursor(state,notes,group)})
    # Only transport wording changes when retaining a v3 conversation after repair.
    messages[0] = {'role': 'system', 'content': system}
    # Each accepted reply is saved before applying it. Replay a saved but unapplied
    # answer after interruption, without asking a model to translate again.
    for step in range(160):
        if packet['complete']: return
        tool = submission_tool(state, notes, group)
        cursor = question_cursor(state,notes,group)
        prior = [a for a in rows(attempts_path) if a['cursor']==cursor]
        accepted = next((a for a in prior if a.get('accepted')),None)
        if accepted is None:
            # In-flight with no persisted response has unknown billing, never zero.
            if inflight.exists():
                lost = read(inflight)
                append(attempts_path,{**lost,'error':'InterruptedUnknownUsage','usage':None,'seconds':0})
                inflight.unlink()
                prior = [a for a in rows(attempts_path) if a['cursor']==cursor]
            cohort_prior = [a for a in prior if a.get('transport')==VERSION]
            for attempt in range(len(cohort_prior),2):
                rec = dict(cursor=cursor,attempt=attempt,model=model,messages=messages,tools=[tool],
                           role_markdown=role,transport=VERSION,usage=None,started=time.time())
                save(inflight,rec)
                start=time.monotonic()
                try:
                    response=await client.chat.completions.create(model=model,messages=messages,tools=[tool],
                        tool_choice={'type':'function','function':{'name':'submit'}},temperature=.95,top_p=.1,
                        max_tokens=1024,extra_body={'thinking':{'type':'disabled'}})
                    rec['response']=response.choices[0].message.model_dump(exclude_none=True)
                    u=response.usage
                    if u:
                        rec['usage']=dict(input_tokens=u.prompt_tokens,output_tokens=u.completion_tokens,total_tokens=u.total_tokens,
                            reasoning_tokens=getattr(u.completion_tokens_details,'reasoning_tokens',None),
                            cached_tokens=getattr(getattr(u,'prompt_tokens_details',None),'cached_tokens',None))
                    calls=response.choices[0].message.tool_calls
                    if not calls or len(calls)!=1 or calls[0].function.name!='submit': raise ValueError('Expected one submit call')
                    args=json.loads(calls[0].function.arguments)
                    if set(args)!={'answer'} or args['answer']=='ready': raise ValueError('Invalid answer envelope')
                    value = args['answer']
                    if not isinstance(value, (str, dict)): raise ValueError('Invalid answer type')
                    answer = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
                    rec['answer']=answer
                    # Validate on a temporary lane to avoid marking malformed answers
                    # accepted. Only public state is copied; no hidden deltas exist here.
                    import tempfile
                    with tempfile.TemporaryDirectory() as temp:
                        source=state['event_dir'](notes,state['current_task'](notes))/group
                        for p in source.iterdir():
                            if p.is_file(): shutil.copyfile(p,Path(temp)/p.name)
                        engine=runpy.run_path(str(Path(state['ROOT'])/'scripts/questionnaires'/f'{group}.py'))
                        rendered=engine['main'](answer,Path(temp))
                        if rendered.startswith('答案格式错误'): raise ValueError('Unusable questionnaire answer')
                    rec['accepted']=True
                except Exception as exc:
                    rec['error']=type(exc).__name__ # no secrets or endpoint text
                rec['seconds']=time.monotonic()-start
                append(attempts_path,rec);inflight.unlink(missing_ok=True)
                if rec.get('accepted'): accepted=rec;break
            if accepted is None: raise RuntimeError(f'{group}: two unusable attempts; inspect {attempts_path}')
        next_packet=state['answer'](group,accepted['answer'],notes)
        message=accepted['response']
        messages += [message,{'role':'tool','tool_call_id':message['tool_calls'][0]['id'],
                              'content':json.dumps(next_packet,ensure_ascii=False)}]
        save(history_path,{'messages':messages,'cursor':question_cursor(state,notes,group)})
        packet=next_packet
        usage=accepted.get('usage') or {}
        if usage.get('input_tokens',0)>20000 or (usage.get('reasoning_tokens') or 0)>512:
            raise RuntimeError('Cost guard: accepted answer retained; inspect before continuation')
    raise RuntimeError('Question count guard reached')


def render_report(output, cases, predictions, base, manifest):
    attempts=[a for p in sorted((output/'events').glob('*/*/attempts.jsonl')) for a in rows(p)]
    active=[c for c in cases if c.case_id in predictions]
    summary=dict(completed=len(active),remaining=len(cases)-len(active),usage=usage_summary(attempts),
                 roles={g:usage_summary([a for p in (output/'events').glob(f'*/{g}/attempts.jsonl') for a in rows(p)]) for g in GROUPS})
    if active: summary['scores']=scores(active,predictions)
    save(output/'metrics.json',summary)
    text=['# 平行指标问卷 benchmark','',f"模型：{manifest['model']}；seed：{manifest['seed']}；完成 {len(active)}/{len(cases)}。",
          '五个独立上下文 O/N/S/HW/R，同一公开题面；脚本汇总。D 不翻译；GT 只进入评分器。',
          'R 权重5，其他指标权重1，D 权重0；六指标准确率包含零值，±0.5容差；选项正确须六指标全对。',
          '','| 版本 | 事件 | cosine % | 指标正确 % | 选项全对 % | FP | FN | 遗漏 | 反向 |',
          '|---|---:|---:|---:|---:|---:|---:|---:|---:|']
    matched=read(output/'baseline-deepseek.json') if (output/'baseline-deepseek.json').exists() else None
    comparison=[('新版 deepseek-flash',summary.get('scores')),('旧 dictionary 历史最佳',base['scores'])]
    if matched: comparison.append(('旧 dictionary deepseek-flash',matched['scores']))
    for name,score in comparison:
        if score:
            text.append(f"| {name} | {score['events']} | {score['cosine_pct']:.2f} | {score['metric_accuracy_pct']:.2f} | {score['option_accuracy_pct']:.2f} | {score['fp']} | {score['fn']} | {score['omissions']} | {score['reversals']} |")
    text += ['',f"旧版基线：{base['run']['run_id']}，全量 cosine {base['run']['direction_score']*100:.2f}%；按全量成绩选定后再取同题切片。",
             f"历史最佳账本模型：{base.get('recorded_models','未记录')}；与本次 deepseek-flash 不同，不能把差值全归因于翻译策略。",
             '旧版原始回答直接复用，无新增翻译。旧版提示/批次/前情可能不同，因此这是历史结果对照，不是受控同提示实验。',
             '旧版全量数据已参与迭代，本次20题也是样本内评估，不能证明泛化。','',
             '| 角色 | 调用 | 输入 token | 输出 token | 推理 token（已知） | usage 缺失调用 |',
             '|---|---:|---:|---:|---:|---:|']
    token_rows=[*summary['roles'].items(),('新版合计',summary['usage']),('旧版历史最佳结果行归属',base['usage'])]
    if matched: token_rows.append(('旧版 deepseek 结果行归属',matched['usage']))
    for g,u in token_rows:
        calls='未核定' if g.startswith('旧版') else u['calls']
        reasoning='未知' if u['unavailable_reasoning_calls']==u['calls'] else u['reasoning_tokens']
        text.append(f"| {g} | {calls} | {u['input_tokens']} | {u['output_tokens']} | {reasoning} | {u['unavailable_usage_calls']} |")
    text += ['',base['usage_note'],f"新版 reasoning 缺失调用：{summary['usage']['unavailable_reasoning_calls']}；重试 {summary['usage']['retries']}（自动 {summary['usage']['automatic_retries']}、修复格式后定向重试 {summary['usage']['targeted_retries']}），失败 {summary['usage']['failed_calls']}。",'模型秒数为各调用耗时之和，平行执行不能将它当墙钟耗时。',
             '', '## 新版逐指标', '', '| 指标 | 正确/单元数 | 单指标 cosine % | 遗漏 | 反向 | 零值误报 |', '|---|---:|---:|---:|---:|---:|']
    for m,p in summary.get('scores',{}).get('per_metric',{}).items():
        text.append(f"| {m} | {p['exact']}/{p['cells']} | {100*p['cosine_sum']/p['cells']:.2f} | {p['omissions']} | {p['reversals']} | {p['zero_fp']} |")
    text += ['', 'W 行只作诊断，不计入汇总遗漏/反向。', '', '## 新版错误事件','']
    for row in summary.get('scores',{}).get('failing',[]):text.append(f"- {row['title']}：选项 {row['wrong_options']}")
    (output/'benchmark.md').write_text('\n'.join(text)+'\n')
    return summary


async def run(args):
    OUTPUT_ROOT.mkdir(parents=True,exist_ok=True)
    lock=(OUTPUT_ROOT/'.questionnaire-benchmark.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    cases,dataset=await load_cases(args.seed,args.limit,strict_history=False)
    source=Path(args.solution)/'skills'/args.skill_id
    files=sorted([*source.glob('scripts/*.py'),*source.glob('scripts/questionnaires/*.*'),*source.glob('stages/analyse_*.md')])
    hashes={str(p.relative_to(source)):hashlib.sha256(p.read_bytes()).hexdigest() for p in files if p.is_file()}
    model=configured_model_name()
    if model != JIUWEN_PLAYER_MODEL or model != 'deepseek-flash':raise ValueError('Player model must be deepseek-flash')
    manifest=dict(version=VERSION,seed=args.seed,model=model,source_hashes=hashes,
                  case_ids=[c.case_id for c in cases],dataset=dataset,temperature=.95,top_p=.1,thinking='disabled',
                  context='fresh per event/group; growing O question history; flat roles one question',
                  transport='direct API forced submit tool; ready executed by wrapper; no Jiuwen workspace')
    output=OUTPUT_ROOT/(args.resume_run or (datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')+'-questionnaires'))
    if output.exists():
        previous = read(output/'manifest.json')
        # Resume the frozen candidate, even if live scripts have since changed.
        for name,digest in previous['source_hashes'].items():
            if hashlib.sha256((output/'runtime'/name).read_bytes()).hexdigest()!=digest:
                raise ValueError('Frozen candidate changed; resume refused')
        manifest['source_hashes']=previous['source_hashes']
        if previous != manifest:
            if not args.continue_transport or {k:v for k,v in previous.items() if k!='version'} != {k:v for k,v in manifest.items() if k!='version'}:
                raise ValueError('Resume source/configuration/case pool changed')
            append(output/'transport-cohorts.jsonl',{'previous':previous,'current':manifest,
                   'reason':'Analyst inspected first-event O number-plus-label and R prefixed-option-key failures; current-question typed submit schema only. Frozen policy and every accepted answer retained.'})
            save(output/'manifest.json',manifest)
    else:
        output.mkdir();save(output/'manifest.json',manifest)
        for p in files:
            dest=output/'runtime'/p.relative_to(source);dest.parent.mkdir(parents=True,exist_ok=True);shutil.copyfile(p,dest)
        save(output/'baseline.json',baseline(cases,OUTPUT_ROOT/'benchmark.sqlite3',args.baseline_run))
    if not (output/'baseline-deepseek.json').exists():
        with sqlite3.connect(OUTPUT_ROOT/'benchmark.sqlite3') as db:
            for candidate, in db.execute("SELECT run_id FROM runs WHERE mode='dev-full' AND completed_at IS NOT NULL ORDER BY direction_score DESC"):
                models={m for (body,) in db.execute('SELECT usage_json FROM results WHERE run_id=?',(candidate,))
                        for m in json.loads(body).get('by_model',{})}
                if models=={model}:
                    save(output/'baseline-deepseek.json',baseline(cases,OUTPUT_ROOT/'benchmark.sqlite3',candidate))
                    break
    harness=output/(VERSION+'.py')
    if not harness.exists():shutil.copyfile(Path(__file__),harness)
    base=read(output/'baseline.json')
    state=runpy.run_path(str(output/'runtime/scripts/translation-state.py'))
    predictions=read(output/'predictions.json') if (output/'predictions.json').exists() else {}
    print(json.dumps({'run':output.name,'path':str(output),'completed':len(predictions)},ensure_ascii=False),flush=True)
    env=dotenv_values(jiuwenswarm_env_path())
    async with AsyncOpenAI(api_key=env['API_KEY'],base_url=env['API_BASE'],timeout=120,max_retries=0) as client:
        for index,case in enumerate(cases,1):
            if case.case_id in predictions:continue
            directory=output/'events'/f'{index:03d}';notes=directory/'notebooks';notes.mkdir(parents=True,exist_ok=True)
            state['save'](notes/'workflow-state.json',{'session_id':'benchmark-'+output.name})
            observation={**case.observation,'current_state':{**case.observation['current_state'],'session_id':'benchmark-'+output.name}}
            # Numeric aggregation uses the same event/energy receipt as live observe.
            # This synthetic public state is never added to translator messages.
            state['save'](notes/'review-context.json',{'event_key':f'{index:05d}',
                         'current_state':observation['current_state']})
            state['prepare'](notes,observation,f'{index:05d}')
            start=time.monotonic()
            outcomes=await asyncio.gather(*(translate_group(client,model,state,notes,g,directory) for g in GROUPS),return_exceptions=True)
            errors=[str(x) for x in outcomes if isinstance(x,BaseException)]
            if errors:
                render_report(output,cases,predictions,base,manifest)
                raise RuntimeError('; '.join(errors))
            result=state['option_metrics'](notes)
            predictions[case.case_id]={str(o['choice']):o['metrics'] for o in result}
            save(output/'predictions.json',predictions)
            save(directory/'comparison.json',dict(case_id=case.case_id,title=case.shorttitle,expected=case.expected,
                                                  predicted=predictions[case.case_id],wall_seconds=time.monotonic()-start))
            summary=render_report(output,cases,predictions,base,manifest)
            print(json.dumps({'completed':len(predictions),'total':len(cases),'usage':summary['usage']},ensure_ascii=False),flush=True)
            # Automatic first-event gate retains results for analyst inspection.
            if args.pilot and len(predictions)==1:break
    render_report(output,cases,predictions,base,manifest)
    return output
