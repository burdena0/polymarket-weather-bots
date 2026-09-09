"""Persistent weather signal supervisor. Default operation is disarmed.

The live flag is a user-run operational choice, not enabled by credential entry.
STOP prevents new orders. Unknown prior orders must reconcile before new ones.
"""
import argparse,hashlib,json,sqlite3,time
from contextlib import nullcontext
from datetime import datetime
from pathlib import Path
from .weather_contract_mapping import match_reference
from .weather_router import WeatherRouter
from .weather_execution import Execution
from .polymarket_us import Client,decimal
from .weather_us_settlement import signed
from .polymarket_us_connection import USConnection
from .weather_us_sources import WeatherSources
from .store import process_lock

class Supervisor:
 def __init__(self,root,execution,sources,*,clock=time.time,allow_cli_basis=False):
  self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True);self.execution=execution;self.sources=sources;self.clock=clock
  self.db=sqlite3.connect(self.root/'signals.sqlite')
  self.db.executescript('CREATE TABLE IF NOT EXISTS config(started REAL); CREATE TABLE IF NOT EXISTS signals(id TEXT PRIMARY KEY,status TEXT,detail TEXT);')
  row=self.db.execute('SELECT started FROM config').fetchone()
  if row is None:
   with self.db:self.db.execute('INSERT INTO config VALUES(?)',(clock(),))
  started=self.db.execute('SELECT started FROM config').fetchone()[0]
  self.router=WeatherRouter(execution,started,allow_cli_basis=allow_cli_basis)
 def close(self):self.db.close()
 def once(self):
  status=dict(at=self.clock(),armed=self.execution.client.allow_orders,decisions=[],error=None)
  if (self.root/'STOP').exists():
   status.update(status='stopped',armed=False)
   return self.save(status)
  try:
   for row in self.execution.journal.unresolved():self.execution.reconcile(row['id'])
   if self.execution.journal.unresolved():
    status['status']='reconciliation_required';return self.save(status)
   broker_positions,expected=self.account_snapshot(status)
   packet=self.sources.activity()
   for signal in sorted(packet['rows'],key=lambda r:r['timestamp'])[-20:]:
    ident=hashlib.sha256(json.dumps(signal,sort_keys=True).encode()).hexdigest()
    with self.db:
     created=self.db.execute("INSERT OR IGNORE INTO signals VALUES(?,'processing',NULL)",(ident,)).rowcount
    if not created:continue
    try:
     if not self.router.started_at<=signal['timestamp']<=self.clock() or self.clock()-signal['timestamp']>120:
      result={'status':'historical_signal_skipped'}
     else:
      reference,markets,received=self.sources.context(signal);match=match_reference(reference,markets)
      if match['status']!='matched':result={'status':match['status']}
      elif not match['execution_eligible'] and not self.router.allow_cli_basis:result={'status':'settlement_source_difference'}
      else:
       if broker_positions is not None:
        slug=match['candidates'][0]['slug']
        if slug not in expected and signed(broker_positions.get(slug,{}).get('netPositionDecimal','0'))!=0:
         raise ValueError('Target market has external holdings')
        if signal['side']=='BUY' and not hasattr(self.execution,'entry_budget'):
         from decimal import Decimal
         balances=self.execution.client.request('GET','/v1/account/balances')['balances']
         usd=[row for row in balances if row.get('currency')=='USD']
         if len(usd)!=1 or min(decimal(usd[0]['buyingPower']),decimal(usd[0]['currentBalance']))<Decimal('45'):
          raise ValueError('Preserve $40 cash reserve plus $5 entry allowance')
       book=self.sources.book(match['candidates'][0]['slug'])
       if (self.root/'STOP').exists():result={'status':'stopped'}
       else:result=self.router.route(signal,reference,markets,book,self.clock(),received)
    except Exception as exc:result={'status':'rejected','reason':str(exc)[:160] if isinstance(exc,ValueError) else type(exc).__name__}
    with self.db:self.db.execute('UPDATE signals SET status=?,detail=? WHERE id=?',(result['status'],json.dumps(result),ident))
    status['decisions'].append({'signal_id':ident,**result})
    if self.execution.journal.unresolved():break
    # A routed submission can change broker holdings immediately. Re-enter the
    # account reconciliation phase before considering another signal.
    if self.execution.client.allow_orders and 'execution' in result:break
   status['status']='observing'
  except Exception as exc:status.update(status='source_error',error=type(exc).__name__)
  return self.save(status)
 def account_snapshot(self,status):
  expected={}
  broker_positions=None
  if self.execution.client.allow_orders:
   from .weather_us_settlement import signed
   from .polymarket_us import decimal
   activities=self.execution.client.request('GET','/v1/portfolio/activities')
   for activity in activities['activities']:
    if activity.get('type')!='ACTIVITY_TYPE_POSITION_RESOLUTION':continue
    slug=activity['positionResolution']['marketSlug']
    if not any(p['slug']==slug for p in self.execution.portfolio()['positions']):continue
    receipt,received=self.sources.settlement(slug)
    self.execution.record_settlement(activity,receipt,received)
   broker=self.execution.client.request('GET','/v1/portfolio/positions')
   if broker.get('eof') is not True or broker.get('nextCursor'):raise ValueError('Complete account positions required')
   broker_positions=broker['positions']
   if self.execution.client.request('GET','/v1/orders/open')['orders']:
    raise ValueError('Outstanding broker orders require reconciliation')
   expected={}
   for position in self.execution.portfolio()['positions']:
    expected[position['slug']]=expected.get(position['slug'],0)+decimal(position['quantity'])*(1 if position['outcome']=='YES' else -1)
   for slug,quantity in expected.items():
    if signed(broker_positions.get(slug,{}).get('netPositionDecimal','0'))!=quantity:raise ValueError('Broker holdings differ from bot')
   status['broker_positions_verified']=True
  return broker_positions,expected
 def save(self,status):
  status['portfolio']=self.execution.portfolio()
  tmp=self.root/'status.tmp';tmp.write_text(json.dumps(status,indent=2));tmp.replace(self.root/'status.json');return status

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--root',default='data/us-weather-observer-v1');parser.add_argument('--once',action='store_true');parser.add_argument('--live',action='store_true');parser.add_argument('--adapt-cli-basis',action='store_true');args=parser.parse_args()
 root=Path(args.root);project=Path(__file__).resolve().parents[1]
 with process_lock(root/'supervisor'), (process_lock(project/'data/connections/polymarket-us-execution') if args.live else nullcontext()):
  if (root/'MIGRATED_TO_COMBINED').exists():raise ValueError('This journal was migrated; use the combined supervisor')
  mode=root/'mode.json';root.mkdir(parents=True,exist_ok=True)
  config={'live':args.live,'adapt_cli_basis':args.adapt_cli_basis}
  if mode.exists() and json.loads(mode.read_text())!=config:raise ValueError('Use a separate root when changing mode or settlement policy')
  mode.write_text(json.dumps(config))
  client=USConnection(project/'data/connections').client(allow_orders=True) if args.live else Client('unused','',allow_orders=False)
  execution=Execution(root/'orders.sqlite',client);supervisor=Supervisor(root,execution,WeatherSources(root/'source'),allow_cli_basis=args.adapt_cli_basis)
  stop_at=datetime.fromisoformat(json.loads((project/'routines/schedule.json').read_text())['stop_after']).timestamp()
  result=None
  try:
   while time.time()<stop_at and not (root/'STOP').exists():
    result=supervisor.once()
    if args.once:print(json.dumps(result));break
    time.sleep(15)
  finally:
   final=dict(result or dict(decisions=[],error=None))
   final.update(at=time.time(),armed=False,status='stopped')
   supervisor.save(final)
   supervisor.close();execution.close()
if __name__=='__main__':main()
