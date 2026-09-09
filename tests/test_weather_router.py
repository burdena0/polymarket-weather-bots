import unittest
from decimal import Decimal
from datetime import datetime,timezone
from types import SimpleNamespace
from supahtrade.weather_router import WeatherRouter
from supahtrade.reference_activity_capture import WALLET
class RouterTests(unittest.TestCase):
 def setUp(self):
  self.now=1788903000.;self.calls=[]
  self.execution=SimpleNamespace(client=SimpleNamespace(allow_orders=False),portfolio=lambda:{'positions':[]},available=lambda *args:Decimal(5),submit_entry=lambda *args:self.record('buy',args),submit_exit=lambda *args:self.record('sell',args))
  self.router=WeatherRouter(self.execution,self.now-10)
  self.market=dict(slug='tc-temp-laxhigh-2026-09-09-gte89lt90f',description="highest temperature at (KLAX) for 2026-09-09 National Weather Service Climatological Report between 89F and 90F",active=True,closed=False,orderPriceMinTickSize='.01',minimumTradeQty='.01')
  self.reference=dict(station='KLAX',date='2026-09-09',lower_f=89,upper_f=90,source='NWS_CLI',condition_id='condition',token='token')
  self.signal=dict(proxyWallet=WALLET,type='TRADE',timestamp=self.now-1,conditionId='condition',asset='token',outcome='No',side='BUY',price='.4')
  self.book=dict(request_started=self.now-.5,received=self.now,data={'marketData':dict(marketSlug=self.market['slug'],state='MARKET_STATE_OPEN',transactTime=datetime.fromtimestamp(self.now,timezone.utc).isoformat(),bids=[{'px':{'value':'.60','currency':'USD'},'qty':'10'}],offers=[{'px':{'value':'.62','currency':'USD'},'qty':'10'}])})
 def record(self,side,args):self.calls.append((side,args));return {'state':'settled'}
 def route(self):return self.router.route(self.signal,self.reference,[self.market],self.book,self.now,self.now)
 def test_unarmed_builds_correct_no_order_without_sending(self):
  result=self.route();self.assertEqual(result['status'],'ready_unarmed');self.assertEqual(result['order']['price']['value'],'0.60');self.assertEqual(self.calls,[])
 def test_armed_routes_entry_then_exit(self):
  self.execution.client.allow_orders=True;self.route();self.signal['side']='SELL';self.route()
  self.assertEqual([c[0] for c in self.calls],['buy','sell'])
  self.assertEqual(self.calls[1][1][1]['price']['value'],'0.62')
 def test_wrong_wallet_stale_book_and_signal_never_send(self):
  self.signal['proxyWallet']='wrong'
  with self.assertRaises(ValueError):self.route()
  self.signal['proxyWallet']=WALLET;self.book['request_started']=self.now-3
  with self.assertRaises(ValueError):self.route()
  self.book['request_started']=self.now-.5;self.signal['timestamp']=self.now-121
  with self.assertRaises(ValueError):self.route()
  self.assertEqual(self.calls,[])
 def test_source_difference_not_silently_equivalent(self):
  self.reference['source']='hourly_METAR';self.assertEqual(self.route()['status'],'settlement_source_difference')
  self.router.allow_cli_basis=True;self.assertTrue(self.route()['cli_basis_risk'])
 def test_no_position_no_sell(self):
  self.signal['side']='SELL';self.execution.available=lambda *a:Decimal(0)
  self.assertEqual(self.route()['status'],'no_bot_position')
 def test_fractional_partial_fill_exits_tradable_hundredths(self):
  self.signal['side']='SELL';self.execution.available=lambda *a:Decimal('1.237')
  result=self.route()
  self.assertEqual(result['status'],'ready_unarmed')
  self.assertEqual(Decimal(str(result['order']['quantity'])),Decimal('1.23'))
  self.assertEqual(self.calls,[])
 def test_sub_increment_residual_does_not_submit(self):
  self.signal['side']='SELL';self.execution.available=lambda *a:Decimal('.009')
  self.assertEqual(self.route()['status'],'below_market_minimum')
 def test_old_unchanged_book_is_accepted_at_good_price(self):
  self.book['data']['marketData']['transactTime']=datetime.fromtimestamp(self.now-3600,timezone.utc).isoformat()
  self.assertEqual(self.route()['status'],'ready_unarmed')
 def test_price_loss_rejected_even_with_fresh_timestamp(self):
  self.signal['price']='.37'
  with self.assertRaisesRegex(ValueError,'price lost'):self.route()
 def test_two_cent_boundary_and_better_price(self):
  self.signal['price']='.38';self.assertEqual(self.route()['adverse_price_move'],'0.02')
  self.signal['price']='.45';self.assertLess(Decimal(self.route()['adverse_price_move']),0)
 def test_sell_price_loss(self):
  self.signal['side']='SELL';self.signal['price']='.41'
  with self.assertRaisesRegex(ValueError,'price lost'):self.route()
 def test_nanosecond_source_timestamp(self):
  self.book['data']['marketData']['transactTime']=datetime.fromtimestamp(self.now-1,timezone.utc).strftime('%Y-%m-%dT%H:%M:%S')+'.123456789Z'
  self.assertEqual(self.route()['status'],'ready_unarmed')
