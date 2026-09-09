import copy
from datetime import datetime, timedelta, timezone
import unittest
import tempfile
from supahtrade.intraday_weather import day_bounds, summarize_intraday, probability_range


class IntradayTests(unittest.TestCase):
 def setUp(self):
  self.contract={'station':'KNYC','date':'2026-09-09','lower_f':80,'upper_f':81}
  self.start,self.end=day_bounds('KNYC','2026-09-09')
  self.now=self.start+12*3600+1800
  def iso(at):return datetime.fromtimestamp(at,timezone.utc).isoformat()
  self.forecast={'station':'KNYC','received':self.now,'payload':{'properties':{'updateTime':iso(self.now-60),
   'periods':[{'startTime':iso(self.start+h*3600),'endTime':iso(self.start+(h+1)*3600),'temperature':75,'temperatureUnit':'F'} for h in range(12,24)]}}}
  self.obs={'station':'KNYC','received':self.now,'payload':{'features':[{'properties':{
   'station':'https://api.weather.gov/stations/KNYC','timestamp':iso(self.start+h*3600+1200),
   'temperature':{'value':28,'unitCode':'wmoUnit:degC','qualityControl':'V'}}} for h in range(13)]}}
 def summary(self):return summarize_intraday(self.forecast,self.obs,self.contract,self.now)
 def test_same_day_uses_observed_high_and_remaining_forecast(self):
  s=self.summary();self.assertEqual(s['remaining_hours'],12);self.assertAlmostEqual(s['observed_max_f'],82.4)
  low,high=probability_range(s,self.contract,'YES');nlow,nhigh=probability_range(s,self.contract,'NO')
  self.assertAlmostEqual(low,1-nhigh);self.assertAlmostEqual(high,1-nlow)
  self.assertGreater(high,0);self.assertLess(high,.5)
 def test_cli_standard_midnight_in_summer_and_dst(self):
  self.assertEqual(datetime.fromtimestamp(self.start,timezone.utc).hour,5)
  a,b=day_bounds('KNYC','2026-11-01');self.assertEqual(b-a,86400)
 def test_missing_elapsed_hour_rejected(self):
  self.obs['payload']['features'].pop(3)
  with self.assertRaisesRegex(ValueError,'elapsed hours'):self.summary()
 def test_missing_current_forecast_hour_rejected(self):
  self.forecast['payload']['properties']['periods'].pop(0)
  with self.assertRaisesRegex(ValueError,'remaining forecast'):self.summary()
 def test_wrong_station_future_time_and_units_rejected(self):
  original=copy.deepcopy(self.obs)
  for field,value in [('station','https://api.weather.gov/stations/KMIA'),('timestamp','2099-01-01T00:00:00Z')]:
   self.obs=copy.deepcopy(original);self.obs['payload']['features'][0]['properties'][field]=value
   with self.assertRaises(ValueError):self.summary()
  self.obs=copy.deepcopy(original);self.obs['payload']['features'][0]['properties']['temperature']['unitCode']='wmoUnit:degF'
  with self.assertRaises(ValueError):self.summary()
 def test_stale_partial_and_null_readings_rejected(self):
  self.obs['received']-=301
  with self.assertRaisesRegex(ValueError,'receipt'):self.summary()
  self.obs['received']=self.now;self.obs['payload']['pagination']={'next':'next'}
  with self.assertRaisesRegex(ValueError,'pagination'):self.summary()
  self.obs['payload'].pop('pagination');self.obs['payload']['features'][0]['properties']['temperature']['value']=None
  with self.assertRaisesRegex(ValueError,'elapsed hours'):self.summary()
 def test_ended_day_is_not_a_forecast_trade(self):
  self.now=self.end
  with self.assertRaisesRegex(ValueError,'ended'):self.summary()
 def test_latest_observation_stale(self):
  self.obs['payload']['features']=self.obs['payload']['features'][:-2]
  with self.assertRaisesRegex(ValueError,'90 minutes'):self.summary()
 def test_route_same_day_and_identity_changes_with_observations(self):
  from tests.test_weather_router import RouterTests
  from supahtrade.independent_weather_router import plan_entry
  f=RouterTests();f.setUp()
  market={**f.market,'description':f.market['description'].replace('KLAX','KNYC')}
  book=copy.deepcopy(f.book);book.update(request_started=self.now-.1,received=self.now)
  book['data']['marketData']['transactTime']=datetime.fromtimestamp(self.now,timezone.utc).isoformat()
  a=plan_entry(market,self.forecast,book,self.now,self.now,intraday=True,observations=self.obs)
  self.assertIsNotNone(a['selected']);self.assertFalse(a['requires_reference_trade'])
  self.obs['payload']['features'][0]['properties']['temperature']['value']=30
  b=plan_entry(market,self.forecast,book,self.now,self.now,intraday=True,observations=self.obs)
  self.assertNotEqual(a['signal_id'],b['signal_id'])
 def test_observation_pages_follow_only_station_and_stop_at_day_start(self):
  from supahtrade.independent_weather_sources import IndependentSources
  with tempfile.TemporaryDirectory() as root:
   s=IndependentSources(root,clock=lambda:self.now);calls=[]
   def fetch(url):
    calls.append(url)
    if len(calls)==1:return {'features':list(reversed(self.obs['payload']['features'])),
      'pagination':{'next':'https://api.weather.gov/stations/KNYC/observations?cursor=old'}},self.now
    return {'features':[{'properties':{'timestamp':datetime.fromtimestamp(self.start-60,timezone.utc).isoformat()}}],
      'pagination':{'next':'https://api.weather.gov/stations/KNYC/observations?cursor=older'}},self.now
   s.fetch=fetch
   data=s.observations('KNYC','2026-09-09')
   self.assertEqual(len(calls),2);self.assertEqual(len(data['payload']['features']),13)
   s.observations('KNYC','2026-09-09');self.assertEqual(len(calls),2)
 def test_foreign_pagination_rejected(self):
  from supahtrade.independent_weather_sources import IndependentSources
  with tempfile.TemporaryDirectory() as root:
   s=IndependentSources(root,clock=lambda:self.now)
   s.fetch=lambda url:({'features':list(reversed(self.obs['payload']['features'])),
     'pagination':{'next':'https://example.com/steal'}},self.now)
   with self.assertRaisesRegex(ValueError,'page URL'):s.observations('KNYC','2026-09-09')
