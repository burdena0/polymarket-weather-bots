"""Map public reference-wallet purchases to current contract settlement rules."""
import argparse
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.parse import urlencode, urlparse, parse_qs

from .providers import Http


def map_trade(trade, market, captured_at):
    if trade['conditionId'].lower() != market['conditionId'].lower():
        raise ValueError('Condition mismatch')
    tokens = json.loads(market['clobTokenIds'])
    outcomes = json.loads(market['outcomes'])
    if len(tokens) != len(outcomes) or len(set(tokens)) != len(tokens) or trade['asset'] not in tokens:
        raise ValueError('Ambiguous or missing outcome token')
    outcome = outcomes[tokens.index(trade['asset'])]
    if trade.get('outcome') != outcome:
        raise ValueError('Trade outcome mismatch')
    description = market.get('description', '')
    source = market.get('resolutionSource', '')
    parsed = urlparse(source)
    station = parse_qs(parsed.query).get('site', [None])[0] if parsed.hostname in ('weather.gov', 'www.weather.gov') else None
    if station is not None and not re.fullmatch('[A-Za-z0-9]{4}', station):
        raise ValueError('Invalid station identifier')
    date = re.search(r"on (\d{1,2} [A-Za-z]{3} '\d{2})", description)
    units = 'F' if 'degrees Fahrenheit' in description else 'C' if 'degrees Celsius' in description else None
    return dict(condition_id=trade['conditionId'], token=trade['asset'], question=market['question'],
                outcome=outcome, reference_buy_time=trade['timestamp'], reference_buy_price=trade['price'],
                rules_captured_at=captured_at, rules_sha256=hashlib.sha256(description.encode()).hexdigest(),
                station=station.upper() if station else None, units=units,
                observation_day_text=date.group(1) if date else None,
                hourly_only='Show Hourly Data' in description,
                resolution_source=source, historical_rules_verified=False,
                historical_forecast_available=False, execution_eligible=False,
                warning='Current rules fetched after the trade do not prove what rules or forecasts existed at entry.')


def run(activity_path, out, limit=10):
    if not 1 <= limit <= 20:
        raise ValueError('Use a bounded 1-20 contract sample')
    activity = json.loads(Path(activity_path).read_text())
    selected, seen = [], set()
    for trade in sorted(activity, key=lambda r: r['timestamp'], reverse=True):
        if trade.get('type') != 'TRADE' or trade.get('side') != 'BUY' or trade['conditionId'] in seen:
            continue
        seen.add(trade['conditionId'])
        selected.append(trade)
        if len(selected) == limit:
            break
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    results = []
    for index, trade in enumerate(selected):
        response = Http().json('https://gamma-api.polymarket.com/markets?' + urlencode({'condition_ids': trade['conditionId']}))
        received = time.time()
        matches = [m for m in response if m['conditionId'].lower() == trade['conditionId'].lower()]
        if len(matches) != 1:
            raise ValueError('No unambiguous contract')
        market = matches[0]
        (out / f'contract-{index}.json').write_text(json.dumps(dict(trade=trade, market=market, captured_at=received)), encoding='utf-8')
        results.append(map_trade(trade, market, received))
    (out / 'mapped-entries.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    return results


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--activity', required=True)
    parser.add_argument('--out', required=True)
    parser.add_argument('--limit', type=int, default=10)
    args = parser.parse_args()
    print(json.dumps(run(args.activity, args.out, args.limit), indent=2))
