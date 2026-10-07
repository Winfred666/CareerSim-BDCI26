"""Read the isolated calibration artifact; never update live notebooks/models."""


def distribution(model, metric, predicted_delta, revision=None):
    entry = model['metrics'][metric]
    pmf = {int(error): probability for error, probability in
           entry['calibrated_pmfs'][str(predicted_delta)].items()}
    if abs(sum(pmf.values()) - 1) > 1e-10 or any(q < 0 for q in pmf.values()):
        raise ValueError('invalid calibrated conditional probabilities')
    return {'pmf': pmf, 'n': entry['n'], 'revision': revision}
