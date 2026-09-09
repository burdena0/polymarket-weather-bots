"""One user-operated executor for reference and independent weather strategies."""
import argparse
from contextlib import ExitStack, closing
from datetime import datetime
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
import time

from .store import process_lock
from .weather_execution import Execution
from .weather_supervisor import Supervisor
from .independent_weather_supervisor import IndependentSupervisor
from .independent_weather_sources import IndependentSources
from .weather_us_sources import WeatherSources
from .polymarket_us_connection import USConnection
from .intraday_weather import day_bounds

# SQLite accounting sentinel only; cash checks still cap every new order.
CASH_LEDGER_UNITS=2**60


def cash_sizing_upgrade(root,project):
    """Offline, backed-up switch from cumulative spend to cash-constrained sizing."""
    root=Path(root).resolve();project=Path(project).resolve()
    if not (root/'STOP').is_file():raise ValueError('Create combined STOP and wait for exit first')
    with process_lock(project/'data/connections/polymarket-us-execution'),process_lock(root/'supervisor'):
        manifest=json.loads((root/'combined.json').read_text())
        if manifest.get('sizing')=='available_cash_v1':return {'status':'already_upgraded'}
        with closing(sqlite3.connect(root/'orders.sqlite')) as db:
            if db.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:raise ValueError('Journal integrity failure')
            if db.execute("SELECT 1 FROM orders WHERE state NOT IN ('settled','rejected') LIMIT 1").fetchone():raise ValueError('Reconcile pending orders first')
            if db.execute('SELECT budget,halted FROM limits WHERE id=1').fetchone()!=(10000000,0):raise ValueError('Unexpected budget or halted ledger')
            backup=root/('orders-before-cash-sizing-'+str(time.time_ns())+'.sqlite')
            with closing(sqlite3.connect(backup)) as dst:db.backup(dst)
            db.execute('UPDATE limits SET budget=? WHERE id=1',(CASH_LEDGER_UNITS,));db.commit()
        manifest.update(sizing='available_cash_v1',sizing_backup=str(backup),sizing_changed_at=time.time())
        tmp=root/'combined.tmp';tmp.write_text(json.dumps(manifest,indent=2));tmp.replace(root/'combined.json')
        return {'status':'upgraded','backup':str(backup),'trading_started':False,'cash_reserve':'40','per_order_cap':'5'}


class StrategyExecution:
    """Strategy identity around a single executor; never a second budget."""
    def __init__(self, engine, owner, root, clock=time.time):
        self.engine, self.owner, self.root, self.clock = engine, owner, Path(root), clock
        self.contract = None
        engine.journal.db.execute('CREATE TABLE IF NOT EXISTS strategy_claims(station_day TEXT PRIMARY KEY,slug TEXT UNIQUE,owner TEXT,local_id TEXT UNIQUE,contract TEXT)')

    def __getattr__(self, name):
        return getattr(self.engine, name)

    def entry_budget(self):
        from .polymarket_us import decimal
        rows=self.client.request('GET','/v1/account/balances')['balances']
        usd=[r for r in rows if r.get('currency')=='USD']
        if len(usd)!=1:raise ValueError('Unique USD account balance required')
        cash=min(decimal(usd[0]['currentBalance']),decimal(usd[0]['buyingPower']))
        limit=self.journal.db.execute('SELECT budget,halted FROM limits WHERE id=1').fetchone()
        if limit['halted']:raise ValueError('Shared budget halted')
        remaining=Decimal(limit['budget']-self.journal.reserved())/1000000-Decimal('.50')
        return max(Decimal(0),min(self.engine.per_order,cash-40,remaining))

    def set_contract(self, contract):
        day_bounds(contract['station'], contract['date'])
        self.contract = dict(contract)

    def contracts(self):
        return {r['slug']:json.loads(r['contract']) for r in self.journal.db.execute('SELECT * FROM strategy_claims')}

    def available(self, slug, intent):
        row=self.journal.db.execute('SELECT owner FROM strategy_claims WHERE slug=?',(slug,)).fetchone()
        return self.engine.available(slug,intent) if row and row[0]==self.owner else 0

    def submit_exit(self, local_id, order, cap):
        if (self.root/'STOP').exists():raise ValueError('Combined supervisor stopped')
        if not self.available(order['marketSlug'],order['intent']):raise ValueError('Position belongs to another strategy')
        return self.engine.submit_exit(self.owner+':'+local_id,order,cap)

    def submit_entry(self, local_id, order, cap):
        if (self.root/'STOP').exists():raise ValueError('Combined supervisor stopped')
        c=self.contract
        if not c or c['slug']!=order['marketSlug']:raise ValueError('Missing strategy contract identity')
        if self.clock()>=day_bounds(c['station'],c['date'])[1]:raise ValueError('Climate day ended')
        ident=self.owner+':'+local_id
        key=c['station']+':'+c['date']
        if self.journal.db.execute("SELECT 1 FROM orders WHERE state IN ('prepared','inflight','unknown') LIMIT 1").fetchone():
            raise ValueError('Reconcile shared pending orders first')
        if Decimal(str(cap))>self.entry_budget():raise ValueError('Order exceeds fresh cash or remaining shared budget')
        with self.journal.atomic():
            if self.journal.db.execute("SELECT 1 FROM orders WHERE state IN ('prepared','inflight','unknown') LIMIT 1").fetchone():
                raise ValueError('Reconcile shared pending orders first')
            # Persist before dispatch. A crash here conservatively consumes the
            # station/day claim; it must never cause an automatic resend.
            if self.journal.db.execute('SELECT 1 FROM strategy_claims WHERE station_day=? OR slug=?',(key,c['slug'])).fetchone():
                raise ValueError('Station-day already claimed by a strategy')
            self.journal.db.execute('INSERT INTO strategy_claims VALUES(?,?,?,?,?)',(key,c['slug'],self.owner,ident,json.dumps(c)))
        return self.engine.submit_entry(ident,order,cap)


def prepare(project, independent, reference, target):
    """Offline SQLite backup migration. Never activates trading or clears STOP."""
    project,independent,reference,target=map(lambda p:Path(p).resolve(),(project,independent,reference,target))
    if target.exists():raise ValueError('Target already exists; do not repeat migration')
    if len({independent,reference,target})!=3:raise ValueError('Separate journal paths required')
    for old in (independent,reference):
        if not (old/'STOP').is_file():raise ValueError('Create STOP in both old roots, then wait for processes to exit')
        if (old/'MIGRATED_TO_COMBINED').exists():raise ValueError('Source already migrated')
    with ExitStack() as stack:
        stack.enter_context(process_lock(project/'data/connections/polymarket-us-execution'))
        for old in (independent,reference):stack.enter_context(process_lock(old/'supervisor'))
        config=json.loads((independent/'mode.json').read_text())
        if config!={'strategy':'independent_nws_intraday_v2','live':True}:raise ValueError('Existing live intraday v2 journal required')
        ref_config=json.loads((reference/'mode.json').read_text())
        if ref_config!={'live':True,'adapt_cli_basis':True}:raise ValueError('Expected existing CLI-basis reference configuration')
        def read_db(path):
            db=stack.enter_context(closing(sqlite3.connect(path.as_uri()+'?mode=ro',uri=True)))
            if db.execute('PRAGMA integrity_check').fetchall()!=[('ok',)]:raise ValueError('Journal integrity check failed')
            return db
        orders=read_db(independent/'orders.sqlite');signals=read_db(independent/'signals.sqlite')
        ref_orders=read_db(reference/'orders.sqlite')
        # Explicitly refuse an unimplemented merge instead of dropping trades.
        if ref_orders.execute('SELECT COUNT(*) FROM orders').fetchone()[0]:raise ValueError('Reference journal has orders; audited ledger merge required')
        if orders.execute("SELECT 1 FROM orders WHERE state NOT IN ('settled','rejected') LIMIT 1").fetchone():raise ValueError('Reconcile pending independent orders before migration')
        if orders.execute('SELECT budget,halted FROM limits WHERE id=1').fetchone()!=(10000000,0):raise ValueError('Unexpected or halted budget')
        target.mkdir();(target/'independent').mkdir();(target/'reference').mkdir()
        (target/'STOP').touch()
        for source,destination in ((orders,target/'orders.sqlite'),(signals,target/'independent/signals.sqlite')):
            with closing(sqlite3.connect(destination)) as dst:source.backup(dst)
        with closing(sqlite3.connect(target/'orders.sqlite')) as db:
            db.execute('CREATE TABLE strategy_claims(station_day TEXT PRIMARY KEY,slug TEXT UNIQUE,owner TEXT,local_id TEXT UNIQUE,contract TEXT)')
            for key,ident,raw in signals.execute('SELECT station_day,signal_id,contract FROM independent_entries'):
                c=json.loads(raw)
                db.execute('INSERT INTO strategy_claims VALUES(?,?,?,?,?)',(key,c['slug'],'independent',ident,raw))
            slugs={json.loads(row[0])['marketSlug'] for row in orders.execute('SELECT intent FROM orders')}
            known={r[0] for r in db.execute('SELECT slug FROM strategy_claims')}
            if not slugs<=known:raise ValueError('Order missing preserved contract identity')
            db.commit()
        # Old launchers refuse retired roots even if someone removes their STOP.
        # Write the completion manifest last; partial migrations cannot start.
        for old in (independent,reference):(old/'MIGRATED_TO_COMBINED').write_text(str(target))
        manifest={'version':1,'live':True,'independent_source':str(independent),'reference_source':str(reference),'prepared_at':time.time()}
        (target/'combined.json').write_text(json.dumps(manifest,indent=2))
    return {'status':'prepared','root':str(target),'trading_started':False,'stop_preserved':True}


def run(project,root,once=False):
    project,root=Path(project).resolve(),Path(root).resolve()
    manifest=json.loads((root/'combined.json').read_text())
    if manifest.get('version')!=1 or manifest.get('live') is not True:raise ValueError('Prepared combined journal required')
    with ExitStack() as stack:
        stack.enter_context(process_lock(project/'data/connections/polymarket-us-execution'))
        stack.enter_context(process_lock(root/'supervisor'))
        for name in ('independent','reference'):stack.enter_context(process_lock(root/name/'supervisor'))
        budget=str(Decimal(CASH_LEDGER_UNITS)/1000000) if manifest.get('sizing')=='available_cash_v1' else '10'
        engine=Execution(root/'orders.sqlite',USConnection(project/'data/connections').client(allow_orders=True),budget=budget)
        stack.callback(engine.close)
        independent=IndependentSupervisor(root/'independent',StrategyExecution(engine,'independent',root),IndependentSources(root/'independent/source'),intraday=True)
        reference=Supervisor(root/'reference',StrategyExecution(engine,'reference',root),WeatherSources(root/'reference/source'),allow_cli_basis=True)
        stack.callback(independent.close);stack.callback(reference.close)
        # Do not replay reference activity from before this coordinator startup.
        reference.router.started_at=time.time()
        deadline=datetime.fromisoformat(json.loads((project/'routines/schedule.json').read_text())['stop_after']).timestamp()
        reports={}
        try:
            while time.time()<deadline and not (root/'STOP').exists():
                for name,supervisor in (('reference',reference),('independent',independent)):
                    if (root/'STOP').exists() or time.time()>=deadline:break
                    reports[name]=supervisor.once()
                if once:break
                for _ in range(15):
                    if (root/'STOP').exists() or time.time()>=deadline:break
                    time.sleep(1)
        finally:
            for name,supervisor in (('reference',reference),('independent',independent)):
                supervisor.save({**reports.get(name,{}),'at':time.time(),'status':'stopped','armed':False})


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root',default='data/us-weather-combined-v1')
    p.add_argument('--prepare',action='store_true')
    p.add_argument('--upgrade-cash-sizing',action='store_true')
    p.add_argument('--live',action='store_true')
    p.add_argument('--once',action='store_true')
    a=p.parse_args();project=Path(__file__).resolve().parents[1]
    if a.upgrade_cash_sizing:
        if a.live or a.prepare:p.error('Upgrade must run separately')
        print(json.dumps(cash_sizing_upgrade(a.root,project),indent=2))
    elif a.prepare:
        if a.live: p.error('Prepare and live are separate steps')
        print(json.dumps(prepare(project,project/'data/us-weather-independent-user-live-v1',project/'data/us-weather-live-v1',a.root),indent=2))
    elif a.live:run(project,a.root,a.once)
    else:p.error('Choose --prepare for offline migration or --live to run the prepared coordinator')

if __name__=='__main__':main()
