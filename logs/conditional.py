#!/usr/bin/env python3
"""Revision-conditioned integer error PMF with explicit sparse-data backoff."""
import collections
import json
from pathlib import Path

METRICS = ('O','N','S','W','H')
BASE = Path(__file__).parent / 'results'


def normalize(counts):
    n=sum(counts.values())
    return {int(k):v/n for k,v in counts.items()}


def mix(local, prior, strength):
    n=sum(local.values())
    return {e:(local.get(e,0)+strength*prior.get(e,0))/(n+strength)
            for e in sorted(set(local)|set(prior))}


def distribution(model, metric, predicted_delta, revision=None):
    """Returns P(actual-predicted=e); revision IDs must include namespace."""
    m=model['metrics'][metric]
    global_p=normalize(m['global_counts'])
    prior=mix({int(k):v for k,v in m['prediction_counts'].get(str(predicted_delta),{}).items()},global_p,20)
    rev=m['revisions'].get(revision)
    if rev is None:
        return {'pmf':prior,'revision_seen':False,'cell_n':0,'backoff':'prediction + metric; revision unavailable'}
    revision_prior=mix({int(k):v for k,v in rev['counts'].items()},prior,20)
    cell={int(k):v for k,v in rev['prediction_counts'].get(str(predicted_delta),{}).items()}
    return {'pmf':mix(cell,revision_prior,10),'revision_seen':True,'cell_n':sum(cell.values()),
            'backoff':'revision/prediction -> revision/global-prediction -> metric'}


def main():
    model={'schema':1,'error_definition':'nominal actual delta - predicted delta',
           'metrics':{},'prior_strengths':{'prediction':20,'revision':20,'cell':10},
           'warning':'Exploratory shrinkage, strengths not tuned or held-out validated. Marginals only, not a joint model. Historical curated revisions are not current-policy calibration.'}
    records=[]
    for r in json.loads((BASE/'pairs.json').read_text()):
        for m in METRICS:
            records.append((m,'runtime:'+r['scripts']['HW' if m in ('W','H') else m],r['predicted'][m],r['actual'][m]-r['predicted'][m]))
    for r in json.loads((BASE/'tmp_pairs.json').read_text()):
        records.append((r['metric'],'benchmark:'+r['revision'],r['predicted'],r['actual']-r['predicted']))
    for m in METRICS:
        rows=[r for r in records if r[0]==m]
        revs={}
        for rev in sorted({r[1] for r in rows}):
            rr=[r for r in rows if r[1]==rev]
            revs[rev]={'n':len(rr),'counts':dict(collections.Counter(r[3] for r in rr)),
                'prediction_counts':{str(p):dict(collections.Counter(r[3] for r in rr if r[2]==p)) for p in sorted({r[2] for r in rr})}}
        model['metrics'][m]={'n':len(rows),'global_counts':dict(collections.Counter(r[3] for r in rows)),
            'prediction_counts':{str(p):dict(collections.Counter(r[3] for r in rows if r[2]==p)) for p in sorted({r[2] for r in rows})},'revisions':revs}
    path=BASE/'conditional_model.json'
    path.write_text(json.dumps(model,ensure_ascii=False,indent=2)+'\n')
    # Round-trip verification: saved JSON keys are strings.
    loaded=json.loads(path.read_text())
    for m in METRICS:
        for rev,rr in loaded['metrics'][m]['revisions'].items():
            for p in range(-3,4):
                result=distribution(loaded,m,p,rev)
                assert abs(sum(result['pmf'].values())-1)<1e-12 and min(result['pmf'].values())>=0
        result=distribution(loaded,m,0,'unknown')
        assert abs(sum(result['pmf'].values())-1)<1e-12
        print(m,loaded['metrics'][m]['n'],'observations,',len(loaded['metrics'][m]['revisions']),'revision IDs; P(e|prediction=0,unknown revision)=',result['pmf'])
    print('Verified all five metric PMFs across every recorded revision and predictions -3..3.')


if __name__=='__main__':main()
