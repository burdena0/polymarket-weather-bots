import copy
import unittest
from datetime import datetime,timedelta,timezone
from tests import test_weather_router as fixture
from supahtrade.independent_weather_router import plan_entry


class IndependentRouterTests(unittest.TestCase):
 def setUp(self):
  f=fixture.RouterTests();f.setUp()
  self.market=f.market;self.book=f.book;self.now=f.now
  start=datetime(2026,9,9,tzinfo=timezone(timedelta(hours=-7)))
  periods=[dict(startTime=(start+timedelta(hours=i)).isoformat(),
                endTime=(start+timedelta(hours=i+1)).isoformat(),
                temperature=70,temperatureUnit='F') for i in range(24)]
  self.forecast={'station':'KLAX','received':self.now,'payload':{'properties':{
   'updateTime':datetime.fromtimestamp(self.now-60,timezone.utc).isoformat(),'periods':periods}}}
 def plan(self,**kwargs):
  return plan_entry(self.market,self.forecast,self.book,self.now,self.now,**kwargs)
 def test_independent_no_candidate_needs_no_reference_wallet(self):
  result=self.plan()
  self.assertEqual(result['selected']['outcome'],'NO')
  self.assertEqual(result['selected']['order']['quantity'],5)
  self.assertFalse(result['requires_reference_trade'])
  self.assertFalse(result['orders_submitted'])
  self.assertFalse(result['model_calibrated'])
 def test_wrong_station_incomplete_day_and_stale_data(self):
  original=copy.deepcopy(self.forecast)
  for mutation in ('station','receipt','hours'):
   self.forecast=copy.deepcopy(original)
   if mutation=='station':self.forecast['station']='KNYC'
   if mutation=='receipt':self.forecast['received']-=301
   if mutation=='hours':self.forecast['payload']['properties']['periods'].pop()
   with self.assertRaises(ValueError):self.plan()
 def test_unavailable_yes_depth_does_not_suppress_no(self):
  self.book['data']['marketData']['offers'][0]['qty']='1'
  result=self.plan()
  self.assertEqual(result['sides'][0]['status'],'rejected')
  self.assertEqual(result['selected']['outcome'],'NO')
 def test_correlated_exposure_blocks_another_bracket(self):
  with self.assertRaisesRegex(ValueError,'Station-day'):
   self.plan(positions=[{'slug':'other','station':'KLAX','date':'2026-09-09'}])
 def test_repeated_input_has_stable_intent_identity(self):
  self.assertEqual(self.plan()['signal_id'],self.plan()['signal_id'])
 def test_stale_book_cannot_produce_order(self):
  self.book['request_started']=self.now-3
  self.assertIsNone(self.plan()['selected'])
