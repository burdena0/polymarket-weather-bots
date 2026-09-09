"""Read-only Polymarket US account preflight. Never activates execution."""
import argparse
import json
import time
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from .polymarket_us import decimal


def check(client, deadline, now=None):
    now=time.time() if now is None else now
    result={'checked_at':now,'orders_enabled':False,'checks':{},'blockers':[],
            'excluded_markets':[],'warnings':[]}
    if client.allow_orders:
        raise ValueError('Preflight requires a read-only client')
    if now>=deadline:
        result['blockers'].append('Experiment deadline reached')
    requests=(('balances','/v1/account/balances'),('positions','/v1/portfolio/positions'),
              ('orders','/v1/orders/open'))
    responses={}
    for name,path in requests:
        try:
            responses[name]=client.request('GET',path)
            result['checks'][name]='read_succeeded'
        except Exception:
            result['checks'][name]='read_failed'
            result['blockers'].append(name+' read failed')
    if 'balances' in responses:
        try:
            usd=[row for row in responses['balances']['balances'] if row.get('currency')=='USD']
            if len(usd)!=1:raise ValueError()
            if min(decimal(usd[0]['buyingPower']),decimal(usd[0]['currentBalance']))<Decimal('45'):
                result['blockers'].append('Insufficient USD for $40 reserve plus $5 entry allowance')
        except Exception:result['blockers'].append('USD balance data invalid or incomplete')
    if 'positions' in responses:
        try:
            page=responses['positions']
            if page.get('eof') is not True or page.get('nextCursor'):
                result['blockers'].append('Position coverage incomplete')
            if not isinstance(page['positions'],dict):raise ValueError()
            # A new laptop journal has no ownership history. Never adopt holdings.
            for slug,position in page['positions'].items():
                quantity=Decimal(str(position['netPositionDecimal']))
                if not quantity.is_finite():raise ValueError()
                if quantity!=0:result['excluded_markets'].append(slug)
            if result['excluded_markets']:
                result['warnings'].append('Existing holdings excluded; never adopted into a fresh bot journal')
        except Exception:result['blockers'].append('Position data invalid or incomplete')
    if 'orders' in responses:
        orders=responses['orders'].get('orders')
        if not isinstance(orders,list):result['blockers'].append('Open-order data invalid')
        elif orders:result['blockers'].append('Open orders exist; reconcile before starting another bot')
    result['status']='blocked' if result['blockers'] else 'account_checks_passed'
    result['scope']='Fresh-journal account readiness only; not strategy, quote or execution verification'
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.parse_args()
    from .polymarket_us_connection import USConnection
    root=Path(__file__).resolve().parents[1]
    deadline=datetime.fromisoformat(json.loads((root/'routines/schedule.json').read_text())['stop_after']).timestamp()
    try:
        client=USConnection(root/'data/connections').client(allow_orders=False)
        result=check(client,deadline)
    except Exception:
        result={'status':'blocked','orders_enabled':False,'blockers':['Saved account credentials unavailable or invalid']}
    print(json.dumps(result,indent=2))
    return 0 if result['status']=='account_checks_passed' else 1


if __name__=='__main__':raise SystemExit(main())
