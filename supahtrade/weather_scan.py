"""Repeatable KLAX weather-value scan; records hypotheses, never submits orders."""
import argparse
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import urlencode

from .providers import Http
from .weather_probability import interval_probability
from .weather_fees import FEE_MODELS, trade_cash, verify_weather_schedule


def bounds(question):
    between = re.search(r'between (\d+)-(\d+)\u00b0F', question)
    single = re.search(r'be (\d+)\u00b0F', question)
    if between and int(between[1]) <= int(between[2]):
        return float(between[1])-.5, float(between[2])+.5
    if single and 'or below' in question:
        return -math.inf, float(single[1])+.5
    if single and 'or higher' in question:
        return float(single[1])-.5, math.inf
    raise ValueError('Unsupported Fahrenheit bracket')


def validate_model(model, target, now):
    run_day = date.fromisoformat(target)-timedelta(days=1)
    initialized = datetime.combine(run_day, datetime.min.time(), tzinfo=timezone.utc)
    if initialized.timestamp()+6*3600 > now:
        raise ValueError('Model run not yet available under assumed release lag')
    if (model.get('forecast_model'), model.get('station'), model.get('target_days_after_init')) != ('gfs_global','KLAX',1):
        raise ValueError('Wrong model, station or lead time')
    if date.fromisoformat(model['training_cutoff']) > run_day-timedelta(days=2):
        raise ValueError('Training data too late')
    return initialized


def model_comparison(empirical_yes, baseline_yes, outcome, ask, fee_model='legacy_flat_1pct'):
    if outcome not in ('Yes','No') or any(not math.isfinite(p) or not 0 <= p <= 1 for p in (empirical_yes,baseline_yes)):
        raise ValueError('Invalid probability comparison')
    if ask is not None and (not math.isfinite(ask) or not 0 <= ask <= 1):
        raise ValueError('Invalid ask')
    empirical = empirical_yes if outcome == 'Yes' else 1-empirical_yes
    baseline = baseline_yes if outcome == 'Yes' else 1-baseline_yes
    cost = trade_cash(1,ask,'BUY',fee_model) if ask is not None else None
    return dict(baseline_probability=baseline,
                baseline_assumed_edge_per_share=baseline-cost if cost is not None else None,
                model_probability_difference=empirical-baseline,
                both_models_clear_five_cent_edge=cost is not None and min(empirical,baseline)-cost >= .05,
                comparison_selects_trades=False)


def scan(target, model_path, station_path, out, fee_model='weather_taker_005'):
    if fee_model not in FEE_MODELS:
        raise ValueError('Unknown fee model')
    model_raw = Path(model_path).read_bytes()
    model = json.loads(model_raw)
    initialized = validate_model(model, target, time.time())
    station = json.loads(Path(station_path).read_text())
    if station['properties']['stationIdentifier'] != 'KLAX':
        raise ValueError('Wrong station coordinates')
    longitude, latitude = station['geometry']['coordinates'][:2]
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    plan = dict(target=target, registered_at=time.time(), model_sha256=hashlib.sha256(model_raw).hexdigest(),
                initialized_at=initialized.isoformat(), fee_model=fee_model, adverse_price=.001,
                actual_match_fee_verified=False,
                calibrated=False, execution_eligible=False,
                comparison_model='raw_forecast_sigma2',comparison_selects_trades=False,
                observation_report_types=model.get('observation_report_types','unspecified'))
    (out/'plan.json').write_text(json.dumps(plan, indent=2), encoding='utf-8')
    http = Http(timeout=8, attempts=1)
    def fetch(name, url):
        payload = http.json(url)
        received = time.time()
        (out/(name+'.json')).write_text(json.dumps(dict(source=url, received=received, payload=payload)), encoding='utf-8')
        return payload, received
    q = dict(latitude=latitude, longitude=longitude, hourly='temperature_2m', models='gfs_global',
             run=initialized.strftime('%Y-%m-%dT%H:%M'), forecast_days=3,
             temperature_unit='fahrenheit', timezone='America/Los_Angeles')
    forecast, forecast_received = fetch('forecast','https://single-runs-api.open-meteo.com/v1/forecast?'+urlencode(q))
    if forecast['hourly_units']['temperature_2m'] != '\u00b0F' or forecast['timezone'] != 'America/Los_Angeles':
        raise ValueError('Unexpected forecast units/timezone')
    hourly = forecast['hourly']
    if len(hourly['time']) != len(hourly['temperature_2m']):
        raise ValueError('Unequal forecast vectors')
    selected = [(t,v) for t,v in zip(hourly['time'],hourly['temperature_2m']) if t.startswith(target+'T')]
    if len(selected) != 24 or {datetime.fromisoformat(t).hour for t,v in selected} != set(range(24)) or any(v is None or isinstance(v,bool) or not math.isfinite(v) for t,v in selected):
        raise ValueError('Incomplete forecast day')
    peak = max(v for t,v in selected)
    slug = 'highest-temperature-in-los-angeles-on-'+date.fromisoformat(target).strftime('%B').lower()+'-'+str(date.fromisoformat(target).day)+'-'+target[:4]
    events, _ = fetch('event','https://gamma-api.polymarket.com/events?'+urlencode({'slug':slug}))
    if len(events) != 1 or events[0]['slug'] != slug or len(events[0]['markets']) > 30:
        raise ValueError('Ambiguous or oversized event')
    results = []
    for market in events[0]['markets']:
        if market.get('closed'):
            continue
        if 'site=klax' not in market['resolutionSource'].lower() or 'Show Hourly Data' not in market['description']:
            raise ValueError('Settlement station or convention differs')
        fee_evidence=None
        if fee_model=='weather_taker_005':
            info,fee_received=fetch('fee-'+market['conditionId'],'https://clob.polymarket.com/clob-markets/'+market['conditionId'])
            fee_evidence=dict(verify_weather_schedule(market,info),received=fee_received)
        lower,upper = bounds(market['question'])
        yes = interval_probability([peak+e for e in model['fitted_errors_f']],lower,upper)
        baseline_yes = interval_probability([peak],lower,upper,2.0)
        tokens,outcomes = json.loads(market['clobTokenIds']),json.loads(market['outcomes'])
        if len(tokens)!=2 or len(set(tokens))!=2 or set(outcomes)!={'Yes','No'}:
            raise ValueError('Not a binary event')
        for token,outcome in zip(tokens,outcomes):
            if not token.isdigit():
                raise ValueError('Invalid token')
            book, received = fetch(token,'https://clob.polymarket.com/book?token_id='+token)
            if book['asset_id']!=token or book['market'].lower()!=market['conditionId'].lower():
                raise ValueError('Wrong book')
            asks = [float(r['price']) for r in book['asks']]
            ask = min(asks) if asks else None
            probability = yes if outcome=='Yes' else 1-yes
            comparison = model_comparison(yes,baseline_yes,outcome,ask,fee_model)
            results.append(dict(question=market['question'],condition_id=market['conditionId'],token=token,outcome=outcome,
                                model_probability=probability,ask=ask,
                                assumed_edge_per_share=probability-trade_cash(1,ask,'BUY',fee_model) if ask is not None else None,
                                book_age_seconds=received-float(book['timestamp'])/1000,received=received,execution_eligible=False,
                                fee_evidence=fee_evidence,
                                **comparison))
    result = dict(target=target,forecast_max_f=peak,forecast_received=forecast_received,model='gfs_global',
                  calibration_established=False,settlement_rounding_verified=False,results=results,
                  realized_pnl=None,model_sha256=plan['model_sha256'],fee_model=fee_model,
                  comparison_model='raw_forecast_sigma2',comparison_selects_trades=False,
                  comparison_warning='Models share the same GFS input; agreement is not independent validation.',
                  observation_report_types=plan['observation_report_types'])
    (out/'signals.json').write_text(json.dumps(result,indent=2),encoding='utf-8')
    return result


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--target',required=True);p.add_argument('--model',required=True)
    p.add_argument('--station',required=True);p.add_argument('--out',required=True)
    p.add_argument('--fee-model',choices=FEE_MODELS,default='weather_taker_005')
    a=p.parse_args()
    r=scan(a.target,a.model,a.station,a.out,a.fee_model)
    print(json.dumps(dict(target=r['target'],forecast_max_f=r['forecast_max_f'],outcomes_scanned=len(r['results']),execution_eligible=False)))
