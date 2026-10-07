#!/usr/bin/env python3
"""Compare paired official rollouts and report uncertainty without fitting rules."""
import argparse
import json
from pathlib import Path
import random
import statistics


def median_interval(scores, planned_looks=1):
    """Exact binomial interval; Bonferroni preserves 95% across planned looks."""
    if type(planned_looks) is not int or planned_looks < 1:
        raise ValueError('planned_looks must be positive')
    values = sorted(scores)
    n = len(values)
    if not n:
        raise ValueError('empty scores')
    denominator, mass, cumulative, k = 1 << n, 1, 0, -1
    for i in range(n//2+1):
        cumulative += mass
        if 40*planned_looks*cumulative > denominator:
            break
        k = i
        mass = mass*(n-i)//(i+1)
    return [values[k],values[n-k-1]] if k>=0 else [None,None]


def read_runs(paths):
    rows, models, policies = {}, set(), set()
    for path in paths:
        data = json.loads(path.read_text())
        meta = data['metadata']
        models.add((meta['sampling'],meta['model']['sha256'],meta['conditional']['sha256']))
        policies.add(tuple(meta[name]['sha256'] for name in
                           ('policy','read_context','translation_error','exported_model')))
        for row in data['runs']:
            key = row['seed'],row['replicate']
            if key in rows:
                raise ValueError(f'duplicate seed/replicate: {key}')
            rows[key] = row
    if len(models)!=1:
        raise ValueError('mixed error models or sampling modes')
    if len(policies)!=1:
        raise ValueError('mixed policy sources in one score group')
    return rows,models


def score_summary(rows, planned_looks=1):
    scores = [r['ending']['quantitative_score'] for r in rows]
    interval = median_interval(scores, planned_looks)
    return dict(n=len(scores),mean=statistics.mean(scores),median=statistics.median(scores),
                median_ci95=interval,median_ci_width=interval[1]-interval[0] if interval[0] is not None else None,
                completed=sum(r['ending']['completed'] for r in rows),
                completion_rate=sum(r['ending']['completed'] for r in rows)/len(scores),
                median_planned_looks=planned_looks)


def paired_summary(baseline,candidate,resamples=2000):
    if baseline.keys()!=candidate.keys():
        raise ValueError('paired comparison requires identical seed/replicate sets')
    keys = sorted(baseline)
    a = [baseline[k]['ending']['quantitative_score'] for k in keys]
    b = [candidate[k]['ending']['quantitative_score'] for k in keys]
    changes = [y-x for x,y in zip(a,b)]
    rng = random.Random(20261005)  # Statistical resampling only; no event selection.
    mean_draws,median_draws = [],[]
    for _ in range(resamples):
        indices = [rng.randrange(len(keys)) for _ in keys]
        mean_draws.append(statistics.mean(changes[i] for i in indices))
        median_draws.append(statistics.median(b[i] for i in indices)-statistics.median(a[i] for i in indices))
    def interval(draws):
        values = sorted(draws)
        return [values[int(.025*len(values))],values[min(len(values)-1,int(.975*len(values)))]]
    return dict(mean_change=statistics.mean(changes),mean_change_ci95=interval(mean_draws),
                median_change=statistics.median(b)-statistics.median(a),median_change_ci95=interval(median_draws),
                wins=sum(x>0 for x in changes),ties=sum(x==0 for x in changes),losses=sum(x<0 for x in changes),
                completion_change=sum(candidate[k]['ending']['completed']-baseline[k]['ending']['completed'] for k in keys),
                bootstrap_resamples=resamples)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--baseline',type=Path,nargs='+',required=True)
    parser.add_argument('--candidate',type=Path,nargs='+',required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--median-looks',type=int,default=1,
                        help='Bonferroni adjustment for predeclared sample-size checks (e.g. 1024/2048/4096).')
    args = parser.parse_args()
    if args.median_looks<1:
        parser.error('median-looks must be positive')
    baseline,amodels = read_runs(args.baseline)
    candidate,bmodels = read_runs(args.candidate)
    if amodels!=bmodels:
        parser.error('baseline and candidate use different error models/sampling modes')
    result = dict(baseline=score_summary(list(baseline.values()),args.median_looks),
                  candidate=score_summary(list(candidate.values()),args.median_looks),
                  paired=paired_summary(baseline,candidate),
                  method='exact binomial order-statistic median CI (Bonferroni for planned looks); paired seed bootstrap for changes')
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()
