import base64
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from tests import test_independent_weather_router as fixtures
from supahtrade.independent_weather_supervisor import IndependentSupervisor
from supahtrade.weather_execution import Execution
from supahtrade.polymarket_us import Client


class IndependentSupervisorTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.root=Path(self.tmp.name)
  self.f=fixtures.IndependentRouterTests();self.f.setUp()
  self.orders={};self.creates=[];self.external=False;self.cash='50';self.fail_send=False
  self.client=Client('synthetic',base64.b64encode(bytes(32)).decode(),allow_orders=True,transport=self.transport)
  self.engine=Execution(self.root/'orders.sqlite',self.client)
  self.sources=SimpleNamespace(inventory=lambda:([self.f.market],self.f.now),
   forecast=lambda *a:self.f.forecast,book=lambda slug:self.f.book)
  self.supervisor=IndependentSupervisor(self.root,self.engine,self.sources,clock=lambda:self.f.now)
 def tearDown(self):
  self.supervisor.close();self.engine.close();self.tmp.cleanup()
 def transport(self,method,path,headers,body):
  if path=='/v1/portfolio/activities':return {'activities':[]}
  if path=='/v1/orders/open':return {'orders':[]}
  if path=='/v1/account/balances':return {'balances':[{'currency':'USD','buyingPower':self.cash,'currentBalance':self.cash}]}
  if path=='/v1/portfolio/positions':
   qty=sum(Decimal(str(o['cumQuantity'])) for o in self.orders.values())
   if self.external:qty+=1
   return {'eof':True,'positions':{self.f.market['slug']:{'netPositionDecimal':str(-qty)}}}
  if path=='/v1/order/preview':
   return {'order':{**json.loads(body)['request'],'commissionNotionalTotalCollected':{'value':'.01','currency':'USD'}}}
  if path=='/v1/orders':
   self.creates.append(json.loads(body))
   if self.fail_send:raise TimeoutError('Synthetic uncertainty')
   request=json.loads(body);ident='fake-'+str(len(self.creates))
   self.orders[ident]={**request,'id':ident,'state':'ORDER_STATE_FILLED','cumQuantity':request['quantity'],
    'avgPx':request['price'],'commissionNotionalTotalCollected':{'value':'.01','currency':'USD'}}
   return {'id':ident}
  if method=='GET' and path.startswith('/v1/order/'):return {'order':self.orders[path.rsplit('/',1)[-1]]}
  raise AssertionError(path)
 def test_entry_without_reference_and_restart_without_duplicate(self):
  r=self.supervisor.once()
  self.assertEqual(r.get('execution',{}).get('state'),'settled',r)
  self.assertEqual(len(self.creates),1)
  self.assertFalse(r['selected']['requires_reference_trade'])
  self.supervisor.close();self.engine.close()
  self.engine=Execution(self.root/'orders.sqlite',self.client)
  self.supervisor=IndependentSupervisor(self.root,self.engine,self.sources,clock=lambda:self.f.now)
  self.supervisor.once();self.assertEqual(len(self.creates),1)
  self.assertEqual(self.engine.portfolio()['positions'][0]['quantity'],'5')
 def test_observer_never_submits(self):
  self.client.allow_orders=False
  r=self.supervisor.once()
  self.assertIsNotNone(r['selected']['selected']);self.assertEqual(self.creates,[])
 def test_external_holdings_block(self):
  self.external=True;r=self.supervisor.once()
  self.assertIn('external holdings',r['decisions'][0]['reason']);self.assertEqual(self.creates,[])
 def test_reserve_blocks_entry(self):
  self.cash='44';r=self.supervisor.once()
  self.assertIn('reserve',r['error']);self.assertEqual(self.creates,[])
 def test_unknown_submission_not_repeated(self):
  self.fail_send=True;self.supervisor.once();self.supervisor.once()
  self.assertEqual(len(self.creates),1);self.assertEqual(len(self.engine.journal.unresolved()),1)
 def test_stop_prevents_submission(self):
  (self.root/'STOP').touch();r=self.supervisor.once()
  self.assertEqual(r['status'],'stopped');self.assertFalse(r['armed']);self.assertEqual(self.creates,[])

class InventoryExpiryTests(unittest.TestCase):
 def test_standard_day_boundary_and_invalid_identity(self):
  from supahtrade.independent_weather_supervisor import current_markets
  from supahtrade.intraday_weather import day_bounds
  from supahtrade.weather_contract_mapping import us_contract
  f=fixtures.IndependentRouterTests();f.setUp()
  c=us_contract(f.market);_,end=day_bounds(c['station'],c['date'])
  self.assertEqual(current_markets([f.market],end-1),([f.market],0))
  self.assertEqual(current_markets([f.market],end),([],1))
  self.assertEqual(current_markets([{}],end),([{}],0))
