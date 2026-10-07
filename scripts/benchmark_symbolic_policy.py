#!/usr/bin/env python3
"""Offline official-engine rollouts with conditional noise on decision inputs.

No API/Player calls. Actual actions settle through the unmodified engine. This
isolates the numeric script; semantic fallback, true R and known caps are explicit
benchmark assumptions, not formal-player or cloud results.
"""
import argparse
import asyncio
from concurrent.futures import ProcessPoolExecutor
from copy import copy, deepcopy
import hashlib
import importlib.util
import json
from pathlib import Path
import random
import runpy
import statistics
import time
import unicodedata
import re

from career_emulator.game import GameEngine
from career_emulator.server.models import parse_level_number

ROOT = Path(__file__).resolve().parents[1]
FIELDS = dict(H='health', D='dignity', S='skill', O='output', N='network', W='wealth', R='hidden_risk')
KEYS = dict(Health='H', Dignity='D', Skill='S', Output='O', Network='N', Wealth='W', HiddenRisk='R')
RESOURCE_FIELDS = ('_resolver','_fixed_events','_failure_evaluator','_salary_applicator',
                   '_promotion_applicator','_talk_applicator','_ending_score_calculator','_event_level_change_texts')


class ErrorModel:
    def __init__(self, path, implementation):
        self.model = json.loads(path.read_text())
        distribution = runpy.run_path(str(implementation))['distribution']
        self.pmfs = {m: {p: distribution(self.model, m, p)['pmf'] for p in range(-3, 4)}
                     for m in self.model['metrics']}
        self.emissions = {
            m: {int(a): {int(p): q for p, q in pmf.items()} for a, pmf in metric['emission_pmfs'].items()}
            for m, metric in self.model['metrics'].items() if 'emission_pmfs' in metric}

    def prediction_pmf(self, metric, actual, mode):
        """Invert P(actual-predicted | predicted), never reverse the error sign."""
        if mode == 'calibrated':
            try:
                return self.emissions[metric][actual]
            except KeyError as exc:
                raise ValueError(f'{metric}: calibrated model has no nominal delta {actual}') from exc
        counts = self.model['metrics'][metric]['prediction_counts']
        prior = {p: 1 if mode == 'uniform' else sum(counts.get(str(p), {}).values()) + 20/7
                 for p in range(-3, 4)}
        weights = {p: prior[p]*self.pmfs[metric][p].get(actual-p, 0) for p in prior}
        total = sum(weights.values())
        if not total:
            raise ValueError(f'{metric}: actual delta {actual} is outside the error model support')
        return {p: w/total for p, w in weights.items()}

    @staticmethod
    def draw(pmf, key):
        u = random.Random(int.from_bytes(hashlib.sha256(key.encode()).digest()[:8], 'big')).random()
        total = 0
        for value, probability in sorted(pmf.items()):
            total += probability
            if u < total:
                return value
        return max(pmf)

    def sample(self, metric, actual, mode, key):
        if mode == 'none':
            return actual
        if mode == 'manual':
            # Additional stress test: condition on the gold value as an anchor,
            # subtract an error draw and clamp to the questionnaire domain. This
            # heuristic is not an exact sample of the original conditional law.
            anchor = max(-3, min(3, actual))
            error = self.draw(self.pmfs[metric][anchor], key)
            return max(-3, min(3, actual-error))
        return self.draw(self.prediction_pmf(metric, actual, mode), key)


class MemoryStore:
    def __init__(self):
        self.sessions, self.logs = {}, []

    async def save_session(self, session):
        self.sessions[session.session_id] = deepcopy(session)

    async def load_session(self, session_id):
        return deepcopy(self.sessions.get(session_id))

    async def append_log(self, entry):
        self.logs.append(entry)


def correction_packet(public, hints, history=()):
    """Match the formal bare-event packet; strip all non-public menu metadata."""
    return dict(current_event={k:v for k,v in (public.get('current_event') or {}).items()
                               if k in ('title','description')},
                **({'event_history': {'steps': list(history)}} if history else {}),
                options=[{k:v for k,v in c.items() if k in ('choice','action','description')}
                         for c in public['choices']],
                constraints={'守红线': hints['守红线'].replace(
                    '禁止选预测削减这些指标的选项','禁止选择会削减这些指标的行为'),
                             '晋升条件': hints['补短板']})


async def rollout(policy, helpers, errors, seed, replicate, sampling, traces=False, confidence_diagnostics=True, resources=None,
                  correction=None):
    engine = GameEngine(monthly_event_limit=3, active_long_chain_limit=1)
    engine.store = MemoryStore()
    if resources is not None:
        for name,value in resources.items():
            setattr(engine,name,value)
    resolver = await engine._get_resolver()
    original = getattr(resolver,'_benchmark_original_sample_roots',resolver._sample_root_nodes)
    resolver._benchmark_original_sample_roots = original

    def seeded(session, limit, existing_event_ids):
        view = copy(session)
        view.session_id = seed
        return original(view, limit, existing_event_ids)

    resolver._sample_root_nodes = seeded
    promotion = await engine._get_promotion_applicator()
    caps = {}
    for req in promotion.requirements.values():
        level = parse_level_number(req.from_level)
        caps[level] = (0, {m: int(req.conditions[FIELDS[m]].threshold * promotion.config.stat_clamp.clamp_factor)
                          for m in 'SON'})
    caps[10] = (0, dict(S=None, O=None, N=None))
    session_id = (await engine.new_game()).session_id
    month, stories, energy_actions = None, 0, 0
    diagnostics = dict(cells=0, errors=0, absolute_error=0, semantic_fallbacks=0, forbidden_fallbacks=0,
                       semantic_routes=0, low_confidence_routes=0, structural_routes=0,
                       model_assessments=0, confident_decisions=0, confident_agreements=0,
                       uncertain_decisions=0, uncertain_agreements=0, assessment_time_us=0)
    trace, histories, corrections = [], {}, []
    for step in range(300):
        observation = await engine.observe(session_id)
        session = await engine.store.load_session(session_id)
        career = session.career
        if not career.alive:
            break
        inputs = None
        if not session.initialization_seen:
            choice = 1
        else:
            state = {m: getattr(career, field) for m, field in FIELDS.items()}
            state['L'] = parse_level_number(career.level)
            if month != career.current_month:
                month, stories, energy_actions = career.current_month, 0, 0
            fixed = bool(session.pending_fixed_event)
            raw = observation.choices if fixed else [dict(c.to_choice(), status_updates=c.status_updates)
                                                     for c in resolver.available_choices(session)]
            options, gold_options = [], []
            event = json.dumps(observation.to_mcp_dict()['current_event'], ensure_ascii=False, sort_keys=True)
            for item in raw:
                if session.pending_fixed_event == 'energy_action' and item.get('energy_cost', 0) > session.quarter_energy_remaining:
                    continue
                actual = dict.fromkeys(FIELDS, 0)
                actual.update({KEYS[k]: v for k, v in item['status_updates'].items() if k in KEYS})
                delta = dict(actual)
                if not fixed:
                    delta['D'] = 0  # No ordinary-event D questionnaire in the submitted solution.
                    for metric in errors.pmfs:
                        key = f'{seed}:{replicate}:{month}:{stories}:{event}:{item["choice"]}:{metric}'
                        delta[metric] = errors.sample(metric, actual[metric], sampling, key)
                        diagnostics['cells'] += int(sampling != 'none')
                        diagnostics['errors'] += int(delta[metric] != actual[metric])
                        diagnostics['absolute_error'] += abs(delta[metric]-actual[metric])
                options.append(dict(choice=item['choice'], metrics=delta, energy_cost=item.get('energy_cost', 0)))
                gold_options.append(dict(choice=item['choice'], metrics=dict(actual, D=actual['D'] if fixed else 0),
                                         energy_cost=item.get('energy_cost', 0)))
            kind = 'energy' if fixed and any(o['energy_cost'] > 0 for o in options) else 'main' if fixed else 'story'
            context = dict(month=month, kind=kind, duration=career.duration_in_level,
                           energy=career.energy, story_actions=stories, energy_actions=energy_actions,
                           bad_reviews=sum('不妙的半年绩效' in row.message for row in engine.store.logs if row.entry_type == 'warning'),
                           risk_bursts=sum('隐患好巧不巧一起炸了' in row.message for row in engine.store.logs if row.entry_type == 'warning'))
            choice = policy['choose'](state, caps, options, context, helpers)
            assessment = None
            if confidence_diagnostics and kind == 'story' and 'confidence' in helpers:
                began = time.perf_counter()
                assessment = helpers['confidence'](state, caps, options, context, choice)
                diagnostics['assessment_time_us'] += round(1e6*(time.perf_counter()-began))
                if assessment['reason']:
                    diagnostics['semantic_routes'] += 1
                    diagnostics['low_confidence_routes' if assessment['samples'] else 'structural_routes'] += 1
                if assessment['samples']:
                    diagnostics['model_assessments'] += 1
                    nominal = policy.get('_choose_nominal', policy['choose'])
                    oracle = nominal(state, caps, gold_options, context, helpers)
                    if oracle is not None and choice is not None:
                        actual_by_choice = {o['choice']: o for o in gold_options}
                        equal = (helpers['project'](state, caps, actual_by_choice[choice]['metrics']) ==
                                 helpers['project'](state, caps, actual_by_choice[oracle]['metrics']))
                        label = 'confident' if assessment['confidence'] >= .5 else 'uncertain'
                        diagnostics[label+'_decisions'] += 1
                        diagnostics[label+'_agreements'] += choice == oracle or equal
            if choice is None:
                # Formal Leader performs semantic reasoning. This deliberately
                # labelled substitute sees noisy inputs only, never gold effects.
                diagnostics['semantic_fallbacks'] += 1
                diagnostics['forbidden_fallbacks'] += 1
                choice = max(options, key=lambda o: (o['metrics']['H'], -o['metrics']['R'], -o['choice']))['choice']
            numeric_choice = choice
            public = observation.to_mcp_dict()
            title = (public.get('current_event') or {}).get('title','')
            chain_key = re.sub(r'\s+', '', unicodedata.normalize('NFKC',title)) if kind=='story' and title else None
            corrected = None
            if correction is not None and assessment is not None and assessment['reason']:
                # The model receives exactly the public handoff; public state/time
                # only distinguish cached contexts. Neither callback input contains
                # translations, routing reasons, gold effects or future scores.
                packet = correction_packet(public, helpers['hints'](state,caps), histories.get(chain_key,()))
                cache_context = {k:v for k,v in public['current_state'].items() if k!='session_id'}
                corrected = await correction(packet,cache_context)
                choice = corrected['choice']
                if type(choice) is not int or choice not in {o['choice'] for o in options}:
                    raise ValueError('Invalid semantic correction choice')
                corrections.append(dict(step=step,cache_key=corrected['cache_key'],choice=choice,
                                        numeric_choice=numeric_choice))
            if chain_key is not None:
                selected_public = next(c for c in public['choices'] if c['choice']==choice)
                histories.setdefault(chain_key,[]).append(dict(
                    description=(public.get('current_event') or {}).get('description',''),current_month=month,
                    selected_choice={k:v for k,v in selected_public.items() if k in ('action','description')}))
            if kind == 'story':
                stories += 1
            elif kind == 'energy':
                energy_actions += 1
            inputs = dict(state=state, context=context, options=options, choice=choice)
            if corrected is not None:
                oracle = policy.get('_choose_nominal',policy['choose'])(state,caps,gold_options,context,helpers)
                inputs['correction'] = dict(corrected,numeric_choice=numeric_choice,oracle_choice=oracle,
                    raw_guard_choices=[o['choice'] for o in helpers['guard'](state,[
                        dict(choice=item['choice'],metrics={m:item['status_updates'].get(k,0)
                            for k,m in KEYS.items()}) for item in raw])],
                    promotion_key_month=month%6==5,
                    risk_recovery_conflict=state['R']>1 and any(o['metrics']['R']<0 for o in options)
                        and any(o['metrics']['R']>=0 and any(o['metrics'][m]>0 for m in 'ONS') for o in options))
            if traces:
                inputs['actual_deltas'] = {str(item['choice']): {KEYS[k]: v for k, v in item['status_updates'].items() if k in KEYS}
                                          for item in raw}
            if assessment is not None:
                inputs['confidence'] = assessment
        action = await engine.take_action(session_id, choice)
        if not action.success:
            raise RuntimeError(action.error)
        if traces and inputs:
            # Keep settled state, without copying the growing lifetime history
            # into every trace row. This only reduces the offline artifact.
            settled = (await engine.store.load_session(session_id)).career.to_dict()
            inputs['actual_state_after'] = {k:v for k,v in settled.items() if k != 'statistics'}
            trace.append(inputs)
    else:
        raise RuntimeError('Step budget exceeded')
    session = await engine.store.load_session(session_id)
    calculator = await engine._get_ending_score_calculator()
    result = dict(seed=seed, replicate=replicate, ending=calculator.compute(session).to_dict(),
                  final_state=session.career.to_dict(), diagnostics=diagnostics, steps=step)
    if correction is not None:
        result['corrections'] = corrections
    if traces:
        result['trace'] = trace
    if resources is not None:
        resources.update((name,getattr(engine,name)) for name in RESOURCE_FIELDS)
    return result


def summary(rows):
    scores = sorted(r['ending']['quantitative_score'] for r in rows)
    return dict(n=len(rows), completed=sum(r['ending']['completed'] for r in rows),
                mean=statistics.mean(scores), median=statistics.median(scores), min=scores[0],
                p10=scores[int(.1*(len(scores)-1))], p25=scores[int(.25*(len(scores)-1))],
                partial_median=statistics.median(r['ending']['competition_partial_score'] for r in rows),
                diagnostics={k: sum(r['diagnostics'][k] for r in rows) for k in rows[0]['diagnostics']})


def load_inputs(policy_path, context_path, model_path, conditional_path):
    api = runpy.run_path(str(context_path))
    helpers = dict(project=api['projected_status'], guard=api['redline_candidates'],
                   safety=api['redline_score'], greedy=api['_greedy_guidance'],
                   requirements=api['PROMOTION_REQUIREMENTS'], low_thresholds=api['LOW_THRESHOLDS'],
                   confidence=api['decision_confidence'],hints=api['decision_hints'])
    return runpy.run_path(str(policy_path)), helpers, ErrorModel(model_path, conditional_path)


def init_worker(policy_path, context_path, model_path, conditional_path, fresh_resources):
    global WORKER_INPUTS, WORKER_RESOURCES
    WORKER_INPUTS = load_inputs(policy_path, context_path, model_path, conditional_path)
    WORKER_RESOURCES = None if fresh_resources else {}


def worker_rollout(task):
    return asyncio.run(rollout(*WORKER_INPUTS, *task, resources=WORKER_RESOURCES))


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--policy', type=Path, default=ROOT/'solution/skills/observe-decide-review/scripts/numeric-policy.py')
    parser.add_argument('--context-script', type=Path, default=ROOT/'solution/skills/observe-decide-review/scripts/read-context.py')
    parser.add_argument('--model', type=Path, default=ROOT/'logs/conditional_model.json')
    parser.add_argument('--conditional', type=Path, default=ROOT/'logs/conditional.py')
    parser.add_argument('--sampling', choices=('historical', 'uniform', 'manual', 'none', 'calibrated'), default='historical')
    parser.add_argument('--seeds', type=int, default=64)
    parser.add_argument('--start-index', type=int, default=0,
                        help='Continue the same cohort with new seeds, without rerunning its prefix.')
    parser.add_argument('--replicates', type=int, default=1)
    parser.add_argument('--cohort', default='validation')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--traces', action='store_true')
    parser.add_argument('--trace-seeds',type=int,
                        help='Keep detailed traces for only the first N seeds; choices and noise are unchanged.')
    parser.add_argument('--workers', type=int, default=1)
    parser.add_argument('--no-confidence-diagnostics', action='store_true',
                        help='Skip diagnostic stability sampling; numeric choices and scores are unchanged.')
    parser.add_argument('--fresh-resources',action='store_true',
                        help='Reload official event/rule resources for every game; sessions/stores are always new.')
    args = parser.parse_args()
    if args.seeds < 1 or args.replicates < 1 or args.workers < 1 or args.start_index < 0:
        parser.error('seeds, replicates and workers must be positive; start-index must be nonnegative')
    if args.trace_seeds is not None and args.trace_seeds<0:
        parser.error('trace-seeds must be nonnegative')
    sources = dict(policy=args.policy, model=args.model, conditional=args.conditional, benchmark=Path(__file__),
                   read_context=args.context_script,
                   translation_error=args.policy.with_name('translation-error.py'),
                   exported_model=args.policy.with_name('translation-errors.json'))
    metadata = {name: dict(path=str(path.resolve()), sha256=hashlib.sha256(path.read_bytes()).hexdigest())
                for name, path in sources.items()}
    metadata.update(sampling=args.sampling, cohort=args.cohort, assumptions=['gold current R', 'known true caps',
                    'ordinary D omitted', 'independent metric marginals', 'numeric semantic substitute; no Player/cloud'],
                    seed_rule='error-policy-20261005-{cohort}-{index:03d}',
                    confidence_oracle='one-step nominal rule with true six-metric deltas; not future-optimal or Leader correctness')
    metadata.update(confidence_diagnostics=not args.no_confidence_diagnostics, workers=args.workers,trace_seeds=args.trace_seeds,
                    start_index=args.start_index,resource_cache=not args.fresh_resources,
                    noisy_option_metrics=list(json.loads(args.model.read_text())['metrics']),
                    calibration=json.loads(args.model.read_text()).get('calibration'))
    rows = []
    args.output.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    tasks = [(f'error-policy-20261005-{args.cohort}-{i:03d}', replicate, args.sampling,
              args.traces and (args.trace_seeds is None or i<args.start_index+args.trace_seeds), not args.no_confidence_diagnostics)
             for i in range(args.start_index,args.start_index+args.seeds) for replicate in range(args.replicates)]
    def record(row):
        rows.append(row)
        if len(rows) % 16 == 0:
            args.output.write_text(json.dumps(dict(metadata=metadata, summary=summary(rows), runs=rows),
                                             ensure_ascii=False, indent=2)+'\n')
            print(len(rows), summary(rows), flush=True)
    paths = (args.policy, args.context_script, args.model, args.conditional)
    if args.workers == 1:
        inputs = load_inputs(*paths)
        resources = None if args.fresh_resources else {}
        for task in tasks:
            record(await rollout(*inputs, *task, resources=resources))
    else:
        with ProcessPoolExecutor(max_workers=args.workers, initializer=init_worker, initargs=(*paths,args.fresh_resources)) as pool:
            for row in pool.map(worker_rollout, tasks):
                record(row)
    metadata['elapsed_seconds'] = round(time.monotonic()-started,3)
    args.output.write_text(json.dumps(dict(metadata=metadata, summary=summary(rows), runs=rows),
                                     ensure_ascii=False, indent=2)+'\n')
    print(summary(rows), 'seconds', metadata['elapsed_seconds'], flush=True)


if __name__ == '__main__':
    asyncio.run(main())
