"""Router-to-journal lifecycle against a deterministic fake broker; no network."""
import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from tests import test_weather_router as fixtures
from supahtrade.polymarket_us import Client
from supahtrade.weather_execution import Execution
from supahtrade.weather_router import WeatherRouter
from supahtrade.weather_supervisor import Supervisor
from types import SimpleNamespace


class WeatherLifecycleTests(unittest.TestCase):
 def test_routed_entry_restart_exit_and_pnl(self):
  self.run_lifecycle('5','-0.12')
 def test_fractional_partial_entry_restart_and_automatic_exit(self):
  self.run_lifecycle('1.23','-0.0446')
 def test_subcent_residual_keeps_position_and_cost_basis(self):
  self.run_lifecycle('1.237',None,'0.007')
 def run_lifecycle(self,filled_quantity,expected_pnl,residual='0'):
  fixture=fixtures.RouterTests();fixture.setUp()
  orders={};creates=[]
  def transport(method,path,headers,body):
   if path=='/v1/portfolio/activities':return {'activities':[]}
   if path=='/v1/orders/open':return {'orders':[]}
   if path=='/v1/account/balances':
    return {'balances':[{'currency':'USD','buyingPower':'50','currentBalance':'50'}]}
   if path=='/v1/portfolio/positions':
    quantity=sum(Decimal(str(o['cumQuantity']))*(-1 if '_BUY_' in o['intent'] else 1) for o in orders.values())
    return {'eof':True,'positions':{fixture.market['slug']:{'netPositionDecimal':str(quantity)}}}
   if path=='/v1/order/preview':
    request=json.loads(body)['request']
    return {'order':{**request,'commissionNotionalTotalCollected':{'value':'.01','currency':'USD'}}}
   if path=='/v1/orders':
    request=json.loads(body);ident='order-'+str(len(creates)+1);creates.append(ident)
    orders[ident]={**request,'id':ident,'state':'ORDER_STATE_FILLED',
      'cumQuantity':request['quantity'],'avgPx':request['price'],
      'commissionNotionalTotalCollected':{'value':'.01','currency':'USD'}}
    if '_BUY_' in request['intent'] and filled_quantity!='5':
     orders[ident].update(state='ORDER_STATE_CANCELED',cumQuantity=filled_quantity)
    return {'id':ident}
   if method=='GET' and path.startswith('/v1/order/'):
    return {'order':orders[path.rsplit('/',1)[-1]]}
   raise AssertionError('Unexpected broker request')
  # Synthetic signing material, with all transport replaced above.
  import base64
  client=Client('test',base64.b64encode(bytes(32)).decode(),allow_orders=True,transport=transport)
  with tempfile.TemporaryDirectory() as root:
   path=Path(root)/'orders.sqlite';engine=Execution(path,client)
   try:
    router=WeatherRouter(engine,fixture.now-10)
    signals=[fixture.signal]
    sources=SimpleNamespace(activity=lambda:{'rows':signals},
      context=lambda signal:(fixture.reference,[fixture.market],fixture.now),
      book=lambda slug:fixture.book)
    supervisor=Supervisor(Path(root)/'supervisor',engine,sources,clock=lambda:fixture.now-10)
    def route(signal):
     return router.route(signal,fixture.reference,[fixture.market],fixture.book,fixture.now,fixture.now)
    supervisor.clock=lambda:fixture.now
    try:
     report=supervisor.once()
     self.assertTrue(report['broker_positions_verified'])
     self.assertEqual(report['decisions'][0]['status'],'settled')
    finally:supervisor.close()
    self.assertEqual(engine.portfolio()['positions'][0]['quantity'],filled_quantity)
    engine.close();engine=Execution(path,client);router=WeatherRouter(engine,fixture.now-10)
    with self.assertRaisesRegex(ValueError,'already holds'):
     route(fixture.signal)
    sell={**fixture.signal,'side':'SELL','price':'.38'}
    signals.append(sell)
    supervisor=Supervisor(Path(root)/'supervisor',engine,sources,clock=lambda:fixture.now)
    try:
     report=supervisor.once()
     self.assertTrue(report['broker_positions_verified'])
     self.assertEqual(len(report['decisions']),1)
     self.assertEqual(report['decisions'][0]['status'],'settled')
    finally:supervisor.close()
    portfolio=engine.portfolio()
    if Decimal(residual):
     self.assertEqual(Decimal(portfolio['positions'][0]['quantity']),Decimal(residual))
     self.assertGreater(Decimal(portfolio['positions'][0]['cost']),0)
     self.assertLess(Decimal(portfolio['realized_pnl']),0)
    else:
     self.assertEqual(portfolio['positions'],[])
     self.assertEqual(Decimal(portfolio['realized_pnl']),Decimal(expected_pnl))
    self.assertEqual(engine.journal.unresolved(),[])
    self.assertEqual(route(sell)['status'],'below_market_minimum' if Decimal(residual) else 'no_bot_position')
    self.assertEqual(len(creates),2)
   finally:engine.close()
