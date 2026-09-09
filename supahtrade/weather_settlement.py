"""Idempotent local paper settlement from matching public venue responses."""
import hashlib
import json
from pathlib import Path
import sqlite3
import time
from urllib.parse import urlencode

from .providers import Http


def result_for(state, gamma, clob, received):
    p = state['position']
    if gamma['conditionId'].lower() != p['condition_id'].lower() or clob['condition_id'].lower() != p['condition_id'].lower():
        raise ValueError('Wrong settlement condition')
    gtokens, outcomes = json.loads(gamma['clobTokenIds']), json.loads(gamma['outcomes'])
    ctokens = clob['tokens']
    if len(gtokens) != 2 or len(set(gtokens)) != 2 or len(outcomes) != 2 or len(ctokens) != 2:
        raise ValueError('Expected binary settlement')
    if dict(zip(gtokens, outcomes)) != {t['token_id']:t['outcome'] for t in ctokens}:
        raise ValueError('Outcome mappings disagree')
    if p['token'] not in gtokens or outcomes[gtokens.index(p['token'])] != p['outcome']:
        raise ValueError('Position mapping changed')
    if received <= p['entered_at']:
        raise ValueError('Settlement receipt must follow entry')
    if gamma.get('closed') is not True or gamma.get('umaResolutionStatus') != 'resolved' or clob.get('closed') is not True:
        return dict(status='awaiting_resolution', execution_eligible=False)
    winners = [t['token_id'] for t in ctokens if t.get('winner') is True]
    if len(winners) != 1:
        return dict(status='nonbinary_or_unconfirmed_resolution', execution_eligible=False)
    prices = json.loads(gamma['outcomePrices'])
    expected = ['1' if t == winners[0] else '0' for t in gtokens]
    if len(prices) != 2 or any(str(v) not in ('0', '1') for v in prices) or [str(v) for v in prices] != expected:
        return dict(status='resolution_sources_disagree', execution_eligible=False)
    payout = p['shares'] if p['token'] == winners[0] else 0
    return dict(status='paper_settled', cash=state['cash']+payout, position=None,
                modeled_payout=payout, modeled_redemption_fee=0,
                realized_pnl=state['realized_pnl']+payout-p['cost'], settled_at=received,
                source='matching Gamma resolved prices and CLOB winner; not independent on-chain audit',
                execution_eligible=False, actual_income=0)


def record(db_path, account_raw, gamma, clob, received):
    account_hash = hashlib.sha256(account_raw).hexdigest()
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(db_path)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS settlements(account_hash TEXT PRIMARY KEY, payload TEXT NOT NULL, evidence TEXT NOT NULL)')
        with db:
            db.execute('BEGIN IMMEDIATE')
            existing = db.execute('SELECT payload FROM settlements WHERE account_hash=?', (account_hash,)).fetchone()
            if existing:
                return {**json.loads(existing[0]), 'already_recorded':True}
            result = result_for(json.loads(account_raw), gamma, clob, received)
            if result['status'] == 'paper_settled':
                result['source_account_sha256'] = account_hash
                db.execute('INSERT INTO settlements VALUES(?,?,?)', (account_hash, json.dumps(result),
                           json.dumps(dict(gamma=gamma, clob=clob, received=received))))
            return result
    finally:
        db.close()


def check(root, account_path, run_id):
    raw = Path(account_path).read_bytes()
    state = json.loads(raw)
    p = state['position']
    http = Http(timeout=8, attempts=1)
    rows = http.json('https://gamma-api.polymarket.com/markets?' + urlencode({'condition_ids':p['condition_id']}))
    matches = [r for r in rows if r['conditionId'].lower() == p['condition_id'].lower()]
    if len(matches) != 1:
        raise ValueError('Ambiguous settlement market')
    clob = http.json('https://clob.polymarket.com/markets/'+p['condition_id'])
    received = time.time()
    result = record(Path(root)/'data/weather-settlements.sqlite', raw, matches[0], clob, received)
    out = Path(root)/'artifacts/weather-settlement-checks'
    out.mkdir(parents=True, exist_ok=True)
    with (out/f'run-{run_id}.json').open('x', encoding='utf-8') as stream:
        json.dump(dict(result=result, gamma=matches[0], clob=clob, received=received), stream, indent=2)
    return result
