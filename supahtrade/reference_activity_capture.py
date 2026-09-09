"""Bounded public activity capture with fixed upper timestamp and raw pages."""
import argparse
from collections import Counter
import json
from pathlib import Path
import time
from urllib.parse import urlencode
from .providers import Http

WALLET = '0x9c95da0c1ec3394330998296582c2739cfa752db'


def capture(out, pages=4):
    if not 1 <= pages <= 10:
        raise ValueError('Bounded capture requires 1-10 pages')
    out = Path(out)
    out.mkdir(parents=True, exist_ok=False)
    end = int(time.time())
    plan = dict(wallet=WALLET, end=end, pages=pages, limit=500, execution_eligible=False)
    (out/'plan.json').write_text(json.dumps(plan, indent=2))
    rows = []
    duplicates = 0
    seen = set()
    exhausted = False
    for page in range(pages):
        url = 'https://data-api.polymarket.com/activity?' + urlencode(dict(
            user=WALLET, end=end, limit=500, offset=page*500, sortBy='TIMESTAMP', sortDirection='DESC'))
        started = time.time()
        batch = Http(timeout=12, attempts=1).json(url)
        received = time.time()
        (out/f'page-{page}.json').write_text(json.dumps(dict(url=url, request_started=started, received=received, rows=batch)))
        if not isinstance(batch, list):
            raise ValueError('Invalid activity page')
        for row in batch:
            if row['proxyWallet'].lower() != WALLET or not 0 <= row['timestamp'] <= end:
                raise ValueError('Wallet or timestamp mismatch')
            key = json.dumps(row, sort_keys=True)
            if key in seen:
                duplicates += 1
            else:
                seen.add(key)
                rows.append(row)
        if len(batch) < 500:
            exhausted = True
            break
        time.sleep(.25)
    summary = dict(records=len(rows), duplicate_records=duplicates,
                   type_counts=dict(Counter(r['type'] for r in rows)),
                   earliest=min((r['timestamp'] for r in rows), default=None),
                   latest=max((r['timestamp'] for r in rows), default=None),
                   endpoint_exhausted=exhausted, complete_account_history=False,
                   limitation='Fixed end reduces moving-page drift but late indexing and same-timestamp ordering can still omit records. Redemptions are cash flows, not profit.',
                   realized_pnl=None, execution_eligible=False)
    (out/'activity.json').write_text(json.dumps(rows))
    (out/'summary.json').write_text(json.dumps(summary, indent=2))
    return summary


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--out', required=True)
    parser.add_argument('--pages', type=int, default=4)
    args = parser.parse_args()
    print(json.dumps(capture(args.out, args.pages), indent=2))
