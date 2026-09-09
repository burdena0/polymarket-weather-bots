"""Bounded public source acquisition for the US weather supervisor."""
from datetime import datetime
import math
from urllib.parse import urlencode
from .paper_feed import PublicFeed
from .weather_reference import map_trade
from .weather_scan import bounds
from .nws_cli import STATIONS

class WeatherSources:
 def __init__(self,root):self.feed=PublicFeed(root)
 def activity(self):return self.feed.activity()
 def context(self,signal):
  query=urlencode({'condition_ids':signal['conditionId']})
  rows,_,received=self.feed._fetch('https://gamma-api.polymarket.com/markets?'+query)
  matches=[r for r in rows if r.get('conditionId')==signal['conditionId']]
  if len(matches)!=1:raise ValueError('Reference rules not uniquely resolved')
  market=matches[0];ref=map_trade(signal,market,received)
  if ref['station'] not in STATIONS:
   raise ValueError(f"Reference station {ref['station'] or 'unknown'} has no supported US mapping")
  if ref['units']!='F':raise ValueError('Reference temperature units are not Fahrenheit')
  if not ref['observation_day_text']:raise ValueError('Reference observation date could not be parsed')
  lower,upper=bounds(market['question'])
  reference=dict(condition_id=ref['condition_id'],token=ref['token'],station=ref['station'],
   date=datetime.strptime(ref['observation_day_text'],"%d %b '%y").date().isoformat(),
   lower_f=None if math.isinf(lower) else int(lower+.5),upper_f=None if math.isinf(upper) else int(upper-.5),
   source='NWS_CLI' if 'Climatological Report' in market.get('description','') else 'reference_observations',
   rules_sha256=ref['rules_sha256'])
  markets,_,us_received=self.feed._fetch('https://gateway.polymarket.us/v1/markets?categories=climate&active=true&closed=false&limit=500')
  markets=markets['markets']
  if len(markets)>=500:raise ValueError('Climate inventory requires pagination')
  return reference,markets,min(received,us_received)
 def book(self,slug):
  from .polymarket_us import identifier
  raw,started,received=self.feed._fetch('https://gateway.polymarket.us/v1/markets/'+identifier(slug)+'/book')
  return dict(data=raw,request_started=started,received=received)
 def settlement(self,slug):
  from .polymarket_us import identifier
  raw,_,received=self.feed._fetch('https://gateway.polymarket.us/v1/markets/'+identifier(slug)+'/settlement')
  return raw,received
