"""User-operated independent US weather strategy; default is observation only.

Entries use an uncalibrated NWS sensitivity model and are held to corroborated
settlement. No claim of copying the reference wallet's private reasoning.
"""
import argparse
import json
import time
from contextlib import nullcontext
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from .independent_weather_router import plan_entry
from .independent_weather_sources import IndependentSources
from .weather_contract_mapping import us_contract
from .weather_execution import Execution
from .weather_supervisor import Supervisor
from .polymarket_us import Client,decimal
from .polymarket_us_connection import USConnection
from .weather_us_settlement import signed
from .store import process_lock


def current_markets(markets,now):
 from .intraday_weather import day_bounds
 active=[];expired=0
 for market in markets:
  try:
   contract=us_contract(market)
   _,end=day_bounds(contract['station'],contract['date'])
  except (ValueError,KeyError):
   active.append(market)  # Keep invalid identities visible to the normal validator.
   continue
  if now>=end:expired+=1
  else:active.append(market)
 return active,expired

class IndependentSupervisor(Supervisor):
 def __init__(self,*args,**kwargs):
  self.intraday=kwargs.pop('intraday',False)
  super().__init__(*args,**kwargs)
  self.db.execute('CREATE TABLE IF NOT EXISTS independent_entries(station_day TEXT PRIMARY KEY, signal_id TEXT UNIQUE, contract TEXT)')
  self.db.execute('CREATE TABLE IF NOT EXISTS scan_cursor(id INTEGER PRIMARY KEY CHECK(id=1),offset INTEGER)')
  self.db.execute('CREATE TABLE IF NOT EXISTS independent_cycles(id INTEGER PRIMARY KEY,at REAL,report TEXT)')
  self.db.execute('CREATE TABLE IF NOT EXISTS held_reviews(slug TEXT PRIMARY KEY,at REAL,report TEXT)')
  with self.db:self.db.execute('INSERT OR IGNORE INTO scan_cursor VALUES(1,0)')
 def plan(self,market,received,positions):
  contract=us_contract(market)
  observations=None
  if self.intraday:
   from .intraday_weather import day_bounds
   start,end=day_bounds(contract['station'],contract['date'])
   if self.clock()>=end:raise ValueError('Climate day ended; await CLI settlement')
   if self.clock()>=start:observations=self.sources.observations(contract['station'],contract['date'])
  forecast=self.sources.forecast(contract['station'],contract['date'])
  budget=self.execution.entry_budget() if hasattr(self.execution,'entry_budget') else None
  return plan_entry(market,forecast,self.sources.book(contract['slug']),self.clock(),received,positions,
                    intraday=self.intraday,observations=observations,entry_budget=budget)
 def save(self,status):
  report=super().save(status)
  with self.db:self.db.execute('INSERT INTO independent_cycles(at,report) VALUES(?,?)',(report['at'],json.dumps(report)))
  return report
 def review_holdings(self,positions):
  from .weather_position_review import review_position
  # Copier positions are included in exposure checks but keep their own exit policy.
  own={json.loads(r[0])['slug'] for r in self.db.execute('SELECT contract FROM independent_entries')}
  held=[p for p in positions if p['slug'] in own]
  previous={r[0]:(r[1],json.loads(r[2])) for r in self.db.execute('SELECT slug,at,report FROM held_reviews')}
  if held and self.intraday:
   position=min(held,key=lambda p:previous.get(p['slug'],(0,))[0])
   row=self.db.execute('SELECT signal_id FROM independent_entries WHERE json_extract(contract,\'$.slug\')=?',(position['slug'],)).fetchone()
   baseline=None
   if row:
    saved=self.db.execute("SELECT report FROM independent_cycles WHERE json_extract(report,'$.execution.signal_id')=? AND json_extract(report,'$.selected.signal_id')=? ORDER BY id LIMIT 1",(row[0],row[0])).fetchone()
    if saved:baseline=json.loads(saved[0]).get('selected')
   report=review_position(position,self.sources,self.clock,baseline)
   with self.db:self.db.execute('INSERT OR REPLACE INTO held_reviews VALUES(?,?,?)',(position['slug'],report['reviewed_at'],json.dumps(report)))
   previous[position['slug']]=(report['reviewed_at'],report)
  return [{**previous[p['slug']][1],'age_seconds':round(self.clock()-previous[p['slug']][0])} if p['slug'] in previous else {'slug':p['slug'],'status':'pending_review','automatic_exit':False} for p in held]
 def once(self):
  status=dict(at=self.clock(),armed=self.execution.client.allow_orders,mode='independent_nws_intraday_v2' if self.intraday else 'independent_nws_sensitivity_v1',
              model_calibrated=False,exit_policy='hold_to_corroborated_settlement',decisions=[],error=None)
  if (self.root/'STOP').exists():return self.save({**status,'status':'stopped','armed':False})
  try:
   for row in self.execution.journal.unresolved():self.execution.reconcile(row['id'])
   if self.execution.journal.unresolved():return self.save({**status,'status':'reconciliation_required'})
   broker,expected=self.account_snapshot(status)
   identities={r[0]:json.loads(r[1]) for r in self.db.execute('SELECT signal_id,contract FROM independent_entries')}
   by_slug={c['slug']:c for c in identities.values()}
   if hasattr(self.execution,'contracts'):by_slug.update(self.execution.contracts())
   positions=[]
   for p in self.execution.portfolio()['positions']:
    if p['slug'] not in by_slug:raise ValueError('Missing held contract identity')
    positions.append({**p,**by_slug[p['slug']]})
   status['held_reviews']=self.review_holdings(positions)
   status['held_review_policy']='One independent holding per cycle, oldest review first. Informational only; no automatic exits.'
   markets,received=self.sources.inventory()
   markets,status['expired_markets_skipped']=current_markets(markets,self.clock())
   offset=self.db.execute('SELECT offset FROM scan_cursor WHERE id=1').fetchone()[0]
   ranked=[];visited=0;started=self.clock()
   for i in range(min(20,len(markets))):
    if self.clock()-started>120 or (self.root/'STOP').exists():break
    market=markets[(offset+i)%len(markets)];visited+=1
    try:
     contract=us_contract(market);key=contract['station']+':'+contract['date']
     if self.db.execute('SELECT 1 FROM independent_entries WHERE station_day=?',(key,)).fetchone():
      raise ValueError('Station-day already attempted in this experiment')
     if broker is not None and contract['slug'] not in expected and signed(broker.get(contract['slug'],{}).get('netPositionDecimal','0'))!=0:
      raise ValueError('Target market has external holdings')
     plan=self.plan(market,received,positions)
     status['decisions'].append(plan)
     if plan['selected']:ranked.append(plan)
    except Exception as exc:
     status['decisions'].append({'status':'rejected','slug':market.get('slug'),'reason':str(exc)[:160] if isinstance(exc,ValueError) else type(exc).__name__})
   with self.db:self.db.execute('UPDATE scan_cursor SET offset=? WHERE id=1',((offset+visited)%max(1,len(markets)),))
   if ranked:
    best=max(ranked,key=lambda p:Decimal(p['selected']['edge_after_allowance']))
    # Reacquire rules and price after scanning; earlier snapshots cannot fill.
    fresh,received=self.sources.inventory()
    matches=[m for m in fresh if m['slug']==best['contract']['slug']]
    if len(matches)!=1:raise ValueError('Selected contract disappeared')
    chosen=self.plan(matches[0],received,positions)
    status['selected']=chosen
    if chosen['selected'] and self.execution.client.allow_orders:
     # The account may change while forecasts/books are fetched.
     broker,expected=self.account_snapshot(status)
     slug=chosen['contract']['slug']
     if slug not in expected and signed(broker.get(slug,{}).get('netPositionDecimal','0'))!=0:
      raise ValueError('Target market has external holdings')
     usd=[r for r in self.execution.client.request('GET','/v1/account/balances')['balances'] if r.get('currency')=='USD']
     required=40+Decimal(chosen['selected']['cost_cap']) if hasattr(self.execution,'entry_budget') else 45
     if len(usd)!=1 or min(decimal(usd[0]['currentBalance']),decimal(usd[0]['buyingPower']))<required:
      raise ValueError('Preserve $40 cash reserve plus $5 entry allowance')
     # Account requests can age a quote; refresh again before dispatch.
     chosen=self.plan(matches[0],received,positions)
     status['selected']=chosen
     if chosen['selected'] and not (self.root/'STOP').exists():
      key=chosen['contract']['station']+':'+chosen['contract']['date']
      with self.db:
       claimed=self.db.execute('INSERT OR IGNORE INTO independent_entries VALUES(?,?,?)',
                              (key,chosen['signal_id'],json.dumps(chosen['contract']))).rowcount
      if claimed:
       selected=chosen['selected']
       if hasattr(self.execution,'set_contract'):self.execution.set_contract(chosen['contract'])
       result=self.execution.submit_entry(chosen['signal_id'],selected['order'],selected['cost_cap'])
       status['execution']={'signal_id':chosen['signal_id'],'state':result['state']}
   status['status']='observing'
  except Exception as exc:
   status.update(status='source_error',error=str(exc)[:160] if isinstance(exc,ValueError) else type(exc).__name__)
  if (self.root/'STOP').exists():status.update(status='stopped',armed=False)
  return self.save(status)


def main():
 p=argparse.ArgumentParser(description=__doc__)
 p.add_argument('--root',default='data/us-weather-independent-observer-v1')
 p.add_argument('--intraday',action='store_true',help='Use observation-plus-forecast v2; use weather_upgrade for an existing v1 root')
 p.add_argument('--once',action='store_true');p.add_argument('--live',action='store_true');args=p.parse_args()
 root=Path(args.root);project=Path(__file__).resolve().parents[1]
 with process_lock(root/'supervisor'), (process_lock(project/'data/connections/polymarket-us-execution') if args.live else nullcontext()):
  if (root/'MIGRATED_TO_COMBINED').exists():raise ValueError('This journal was migrated; use the combined supervisor')
  root.mkdir(parents=True,exist_ok=True);path=root/'mode.json'
  config={'strategy':'independent_nws_intraday_v2' if args.intraday else 'independent_nws_sensitivity_v1','live':args.live}
  if path.exists() and json.loads(path.read_text())!=config:raise ValueError('Strategy configuration mismatch; use weather_upgrade to migrate v1 accounting before --intraday')
  if not path.exists() and (root/'orders.sqlite').exists():raise ValueError('Existing journal requires its original strategy configuration')
  path.write_text(json.dumps(config))
  client=USConnection(project/'data/connections').client(allow_orders=True) if args.live else Client('unused','',allow_orders=False)
  engine=Execution(root/'orders.sqlite',client);supervisor=IndependentSupervisor(root,engine,IndependentSources(root/'source'),intraday=args.intraday)
  deadline=datetime.fromisoformat(json.loads((project/'routines/schedule.json').read_text())['stop_after']).timestamp()
  result=None
  try:
   while time.time()<deadline and not (root/'STOP').exists():
    result=supervisor.once()
    if args.once:print(json.dumps(result));break
    for _ in range(60):
     if time.time()>=deadline or (root/'STOP').exists():break
     time.sleep(1)
  finally:
   supervisor.save({**(result or {}),'at':time.time(),'status':'stopped','armed':False})
   supervisor.close();engine.close()

if __name__=='__main__':main()
