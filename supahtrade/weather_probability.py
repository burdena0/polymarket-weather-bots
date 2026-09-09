"""Empirical-error weather probability candidate, evaluated before price use."""
import argparse
from datetime import date, timedelta
import json
import math
from pathlib import Path


def cdf(value):
    return .5 * (1 + math.erf(value / math.sqrt(2)))


def interval_probability(centers, lower, upper, bandwidth=1.0):
    if not centers or not 0 < bandwidth < math.inf or not lower < upper:
        raise ValueError('Invalid probability inputs')
    if not all(math.isfinite(x) for x in centers):
        raise ValueError('Nonfinite model samples')
    return sum(cdf((upper-x)/bandwidth) - cdf((lower-x)/bandwidth) for x in centers)/len(centers)


def evaluate(training, testing):
    test = [r for r in testing['days'] if r['complete']]
    if not test:
        raise ValueError('No complete test days')
    cutoff = min(date.fromisoformat(r['initialized_at'][:10]) for r in test) - timedelta(days=2)
    train = [r for r in training['days'] if r['complete'] and date.fromisoformat(r['target']) <= cutoff]
    if len(train) < 20:
        raise ValueError('Need at least 20 earlier complete days; this is not statistical sufficiency')
    if len({r['target'] for r in train + test}) != len(train + test):
        raise ValueError('Duplicate or overlapping days')
    if {(r['model'], r['station']) for r in train + test} != {('gfs_global', 'KLAX')}:
        raise ValueError('Mixed forecast sources or stations')
    errors = [r['error_observed_minus_forecast_f'] for r in train]
    climatology = [r['observed_hourly_max_f'] for r in train]
    # Exhaustive two-degree bins, not selected from realized winning brackets.
    edges = [-math.inf] + [59.5 + 2*i for i in range(21)] + [math.inf]
    results = []
    for r in test:
        methods = {'empirical_error': ([r['forecast_max_f']+e for e in errors], 1.0),
                   'station_climatology': (climatology, 1.0),
                   'raw_forecast_sigma2': ([r['forecast_max_f']], 2.0)}
        scores = {}
        for name, (centers, bandwidth) in methods.items():
            probabilities = [interval_probability(centers, a, b, bandwidth) for a, b in zip(edges, edges[1:])]
            actual = [int(a <= r['observed_hourly_max_f'] < b) for a, b in zip(edges, edges[1:])]
            scores[name] = sum((p-y)**2 for p, y in zip(probabilities, actual))
            if abs(sum(probabilities)-1) > 1e-9:
                raise ValueError('Probability mass does not sum to one')
        results.append(dict(target=r['target'], multiclass_brier=scores))
    means = {k:sum(r['multiclass_brier'][k] for r in results)/len(results) for k in results[0]['multiclass_brier']}
    return dict(forecast_model='gfs_global', station='KLAX', target_days_after_init=1,
                training_days=len(train), training_cutoff=str(cutoff), test_days=len(test),
                bandwidth_f=1, mean_multiclass_brier=means, days=results,
                fitted_errors_f=errors, settlement_probabilities_verified=False,
                test_previously_inspected=True, automatic_promotion=False, execution_eligible=False,
                limitations=['Retrospective development scoring; September observations were previously inspected.',
                             'Two-day training embargo is an availability assumption, not a publication audit.',
                             'Two-degree bin boundaries assume rounding conventions not yet reconciled to settlement.',
                             'Seven evaluation days cannot establish robust calibration or trading profitability.'])


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--training', required=True)
    p.add_argument('--testing', required=True)
    p.add_argument('--out', required=True)
    a = p.parse_args()
    result = evaluate(json.loads(Path(a.training).read_text()), json.loads(Path(a.testing).read_text()))
    with Path(a.out).open('x', encoding='utf-8') as stream:
        json.dump(result, stream, indent=2)
    print(json.dumps({k:result[k] for k in ('training_days', 'training_cutoff', 'test_days', 'mean_multiclass_brier')}, indent=2))
