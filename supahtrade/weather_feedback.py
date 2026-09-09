"""Read-only realized-outcome feedback. Failed submissions are not losing bets."""
from contextlib import closing
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from .weather_execution import money, outcome_price


def review(root):
    path=(Path(root)/'orders.sqlite').resolve()
    try:
        with closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True,timeout=1)) as db:
            owners={slug:(owner,json.loads(raw)) for slug,owner,raw in db.execute('SELECT slug,owner,contract FROM strategy_claims')}
            rows=db.execute('SELECT v.snapshot FROM orders o JOIN venue_orders v ON o.id=v.local_id ORDER BY o.rowid').fetchall()
            states=dict(db.execute('SELECT state,COUNT(*) FROM orders GROUP BY state'))
            settlements=[json.loads(r[0]) for r in db.execute('SELECT payload FROM settlements')]
        positions={};closed=[]
        def close_record(slug,side,pnl):
            owner,c=owners.get(slug,('unknown',{}))
            closed.append({'slug':slug,'owner':owner,'station':c.get('station'),'date':c.get('date'),'outcome':side,'realized_pnl':str(pnl)})
        for (raw,) in rows:
            if not raw:continue
            o=json.loads(raw);qty=Decimal(str(o['cumQuantity']))
            if not qty:continue
            side='NO' if o['intent'].endswith('_SHORT') else 'YES';key=(o['marketSlug'],side)
            p=positions.setdefault(key,{'qty':Decimal(0),'cost':Decimal(0),'pnl':Decimal(0)})
            gross=outcome_price(o,'avgPx')*qty;fee=money(o['commissionNotionalTotalCollected'])
            if '_BUY_' in o['intent']:p['qty']+=qty;p['cost']+=gross+fee
            else:
                if qty>p['qty']:raise ValueError('Exit exceeds recorded position')
                basis=p['cost']*qty/p['qty'];p['qty']-=qty;p['cost']-=basis;p['pnl']+=gross-fee-basis
                if p['qty']==0:close_record(key[0],side,p['pnl'])
        for s in settlements:
            key=(s['slug'],s['outcome']);p=positions.get(key)
            if not p or p['qty']!=Decimal(s['quantity']) or p['cost']!=Decimal(s['cost']):raise ValueError('Settlement basis mismatch')
            close_record(s['slug'],s['outcome'],p['pnl']+Decimal(s['realized_pnl']));p['qty']=Decimal(0)
        groups={}
        for r in closed:
            key=r['owner']+':'+str(r['station']);g=groups.setdefault(key,{'closed':0,'wins':0,'losses':0,'breakeven':0,'net':Decimal(0)})
            value=Decimal(r['realized_pnl']);g['closed']+=1;g['net']+=value
            g['wins' if value>0 else 'losses' if value<0 else 'breakeven']+=1
        for g in groups.values():g['net']=str(g['net'])
        return {'available':True,'closed_positions':closed,'groups':groups,
                'open_positions':sum(p['qty']>0 for p in positions.values()),
                'rejected_orders':states.get('rejected',0),
                'pending_orders':sum(states.get(k,0) for k in ('prepared','inflight','unknown')),
                'automatic_model_changes':False,
                'next_step':'Collect resolved outcomes and matched entry forecasts; validate any proposed model change out of sample before enabling it.',
                'scope':'Closed-position realized results only, grouped by strategy and station. Rejected orders are not trading losses. This report does not establish an edge or recalibrate probabilities.'}
    except (OSError,ValueError,KeyError,sqlite3.Error) as exc:
        return {'available':False,'error':type(exc).__name__,'automatic_model_changes':False}
