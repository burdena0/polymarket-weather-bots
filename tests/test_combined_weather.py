import base64
from decimal import Decimal
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from supahtrade.combined_weather import StrategyExecution, prepare
from supahtrade.polymarket_us import Client, limit_order
from supahtrade.weather_execution import Execution
from supahtrade.store import process_lock


class SharedExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
        self.sent=[];self.orders={};self.timeout=False
        self.cash='50'
        client=Client('synthetic',base64.b64encode(bytes(32)).decode(),allow_orders=True,transport=self.transport)
        self.engine=Execution(self.root/'orders.sqlite',client)
        self.ind=StrategyExecution(self.engine,'independent',self.root,clock=lambda:1)
        self.ref=StrategyExecution(self.engine,'reference',self.root,clock=lambda:1)

    def tearDown(self):
        self.engine.close();self.tmp.cleanup()

    def transport(self,method,path,headers,body):
        if path=='/v1/account/balances':return {'balances':[{'currency':'USD','currentBalance':self.cash,'buyingPower':self.cash}]}
        if path=='/v1/order/preview':
            return {'order':{**json.loads(body)['request'],'commissionNotionalTotalCollected':{'value':'.01','currency':'USD'}}}
        if path=='/v1/orders':
            self.sent.append(json.loads(body))
            if self.timeout:raise TimeoutError('synthetic uncertain send')
            ident='fake-'+str(len(self.sent));o=self.sent[-1]
            self.orders[ident]={**o,'id':ident,'state':'ORDER_STATE_FILLED','cumQuantity':o['quantity'],'avgPx':o['price'],'commissionNotionalTotalCollected':{'value':'.01','currency':'USD'}}
            return {'id':ident}
        if path.startswith('/v1/order/'):return {'order':self.orders[path.rsplit('/',1)[-1]]}
        raise AssertionError(path)

    def entry(self,adapter,station='KLAX',date='2026-09-09',suffix='a',price='.20'):
        slug='tc-temp-test-'+suffix
        adapter.set_contract({'station':station,'date':date,'slug':slug})
        return adapter.submit_entry(suffix,limit_order(slug,'YES','BUY',5,price),'4.90')

    def test_more_than_two_positions_fit_shared_budget(self):
        self.entry(self.ind)
        self.entry(self.ref,station='KNYC',suffix='b')
        self.assertEqual(len(self.sent),2)
        self.assertEqual(self.ind.portfolio(),self.ref.portfolio())
        self.entry(self.ref,station='KMIA',suffix='c')
        self.assertEqual(len(self.sent),3)

    def test_same_station_different_bracket_and_restart_cannot_duplicate(self):
        self.entry(self.ind)
        with self.assertRaisesRegex(ValueError,'already claimed'):self.entry(self.ref,suffix='b')
        restarted=StrategyExecution(self.engine,'independent',self.root,clock=lambda:1)
        with self.assertRaisesRegex(ValueError,'already claimed'):self.entry(restarted,suffix='c')
        self.assertEqual(len(self.sent),1)

    def test_reference_cannot_sell_independent_holdings(self):
        self.entry(self.ind)
        order=limit_order('tc-temp-test-a','YES','SELL',5,'.20')
        self.assertEqual(self.ref.available(order['marketSlug'],order['intent']),0)
        with self.assertRaisesRegex(ValueError,'another strategy'):self.ref.submit_exit('sell',order,'.10')
        self.ind.submit_exit('sell',order,'.10')
        self.assertFalse(self.engine.portfolio()['positions'])

    def test_unknown_submission_blocks_other_strategy(self):
        self.timeout=True
        self.assertEqual(self.entry(self.ind)['state'],'unknown')
        with self.assertRaisesRegex(ValueError,'pending'):self.entry(self.ref,station='KMIA',suffix='b')
        self.assertEqual(len(self.sent),1)

    def test_stop_prevents_submission(self):
        (self.root/'STOP').touch()
        with self.assertRaisesRegex(ValueError,'stopped'):self.entry(self.ref)
        self.assertEqual(self.sent,[])

    def test_fresh_cash_decrease_blocks_submission(self):
        self.cash='40.50'
        self.assertEqual(self.ref.entry_budget(),Decimal('.50'))
        with self.assertRaisesRegex(ValueError,'fresh cash'):self.entry(self.ref)
        self.assertEqual(self.sent,[])

    def test_feedback_separates_open_positions_and_closed_loss(self):
        from supahtrade.weather_feedback import review
        self.entry(self.ind)
        r=review(self.root);self.assertTrue(r['available']);self.assertEqual(r['closed_positions'],[])
        self.ind.submit_exit('exit',limit_order('tc-temp-test-a','YES','SELL',5,'.20'),'.10')
        r=review(self.root);self.assertEqual(r['groups']['independent:KLAX']['losses'],1)
        self.assertFalse(r['automatic_model_changes'])

    def test_spent_budget_is_shared_after_positions_close(self):
        self.entry(self.ind,price='.80')
        self.ind.submit_exit('exit-a',limit_order('tc-temp-test-a','YES','SELL',5,'.80'),'.10')
        self.entry(self.ref,station='KNYC',suffix='b',price='.80')
        self.ref.submit_exit('exit-b',limit_order('tc-temp-test-b','YES','SELL',5,'.80'),'.10')
        with self.assertRaisesRegex(ValueError,'reserve|budget'):
            self.entry(self.ind,station='KMIA',suffix='c')
        self.assertEqual(len(self.sent),4)


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.project=Path(self.tmp.name)
        self.ind=self.project/'ind';self.ref=self.project/'ref';self.target=self.project/'combined'
        for root in (self.ind,self.ref):root.mkdir();(root/'STOP').touch()
        (self.ind/'mode.json').write_text(json.dumps({'strategy':'independent_nws_intraday_v2','live':True}))
        (self.ref/'mode.json').write_text(json.dumps({'live':True,'adapt_cli_basis':True}))
        self.engines=[Execution(root/'orders.sqlite',None) for root in (self.ind,self.ref)]
        self.engines[0].journal.prepare('old',{'marketSlug':'tc-temp-old'},100000)
        self.engines[0].journal.claim_dispatch('old')
        self.engines[0].journal.reconcile('old',receipt='synthetic',actual_debit_units=50000)
        with closing(sqlite3.connect(self.ind/'signals.sqlite')) as db:
            db.execute('CREATE TABLE independent_entries(station_day TEXT PRIMARY KEY,signal_id TEXT,contract TEXT)')
            db.execute('INSERT INTO independent_entries VALUES(?,?,?)',('KLAX:2026-09-09','old',json.dumps({'slug':'tc-temp-old','station':'KLAX','date':'2026-09-09'})))
            db.commit()

    def tearDown(self):
        for e in self.engines:e.close()
        self.tmp.cleanup()

    def migrate(self):return prepare(self.project,self.ind,self.ref,self.target)

    def test_backup_preserves_debit_claims_and_old_journal(self):
        old=self.engines[0].journal.get('old');self.migrate()
        self.assertEqual(self.engines[0].journal.get('old'),old)
        with closing(sqlite3.connect(self.target/'orders.sqlite')) as db:
            self.assertEqual(db.execute('SELECT actual_debit FROM orders').fetchone()[0],50000)
            self.assertEqual(db.execute('SELECT owner FROM strategy_claims').fetchone()[0],'independent')
        self.assertTrue((self.target/'STOP').exists())
        self.assertTrue((self.ind/'MIGRATED_TO_COMBINED').exists())

    def test_active_account_lock_refuses_migration(self):
        with process_lock(self.project/'data/connections/polymarket-us-execution'):
            with self.assertRaises(RuntimeError):self.migrate()
        self.assertFalse(self.target.exists())

    def test_cash_upgrade_preserves_orders_and_requires_stop(self):
        from supahtrade.combined_weather import cash_sizing_upgrade,CASH_LEDGER_UNITS
        self.migrate()
        result=cash_sizing_upgrade(self.target,self.project)
        self.assertEqual(result['status'],'upgraded')
        self.assertTrue(Path(result['backup']).is_file())
        with closing(sqlite3.connect(self.target/'orders.sqlite')) as db:
            self.assertEqual(db.execute('SELECT budget FROM limits').fetchone()[0],CASH_LEDGER_UNITS)
            self.assertEqual(db.execute('SELECT actual_debit FROM orders').fetchone()[0],50000)
        self.assertEqual(cash_sizing_upgrade(self.target,self.project)['status'],'already_upgraded')
        (self.target/'STOP').unlink()
        with self.assertRaisesRegex(ValueError,'STOP'):cash_sizing_upgrade(self.target,self.project)

    def test_nonempty_copier_is_not_silently_discarded(self):
        self.engines[1].journal.prepare('ref',{'marketSlug':'tc-temp-x'},100000)
        with self.assertRaisesRegex(ValueError,'audited ledger merge'):self.migrate()
        self.assertFalse(self.target.exists())

    def test_unknown_order_blocks_migration(self):
        j=self.engines[0].journal;j.prepare('uncertain',{'marketSlug':'tc-temp-x'},100000)
        j.claim_dispatch('uncertain');j.mark_unknown('uncertain')
        with self.assertRaisesRegex(ValueError,'pending'):self.migrate()
        self.assertFalse(self.target.exists())
