"""Public, recorded NWS and Polymarket US inputs for independent planning."""
import json
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.parse import urlencode
from datetime import datetime, timezone
from .providers import Http
from .weather_us_sources import WeatherSources
from .nws_cli import STATIONS


class IndependentSources(WeatherSources):
 def __init__(self,root,clock=time.time):
  super().__init__(root)
  self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
  self.clock=clock;self.http=Http(timeout=8,attempts=1)
  self._markets=None;self._forecasts={}
  self._observations={};self._forecast_refreshes={}
 def observations(self,station,target):
  from .intraday_weather import day_bounds
  start,end=day_bounds(station,target);now=self.clock()
  cached=self._observations.get((station,target))
  if cached and 0<=now-cached['received']<120:return cached
  query=urlencode({'start':datetime.fromtimestamp(start,timezone.utc).isoformat(),
                   'end':datetime.fromtimestamp(min(now,end),timezone.utc).isoformat(),'limit':500})
  url=f'https://api.weather.gov/stations/{station}/observations?'+query
  from .forecast_journal import stamp
  features=[];seen=set();previous=None;initial_url=url
  for _ in range(4):
   if url in seen:raise ValueError('Repeated observation cursor')
   parsed=urlparse(url)
   if parsed.scheme!='https' or parsed.hostname!='api.weather.gov' or parsed.path!=f'/stations/{station}/observations':
    raise ValueError('Unexpected observation page URL')
   seen.add(url);page,received=self.fetch(url);rows=page['features']
   if not isinstance(rows,list) or len(rows)>500:raise ValueError('Invalid observation page')
   times=[stamp(r['properties']['timestamp']).timestamp() for r in rows]
   if times!=sorted(times,reverse=True) or (times and previous is not None and times[0]>previous):
    raise ValueError('Observation pages are not chronological')
   features.extend(r for r,t in zip(rows,times) if t>=start)
   next_url=page.get('pagination',{}).get('next')
   if not next_url or (times and times[-1]<=start):break
   if not times:raise ValueError('Empty observation page with continuation')
   previous=times[-1];url=next_url
  else:raise ValueError('Observation history exceeded four pages')
  payload={'features':features,'pagination':{},'coverage_start':start,'pages_read':len(seen)}
  result={'station':station,'received':received,'payload':payload,'source':initial_url}
  self._observations[(station,target)]=result
  return result
 def fetch(self,url,*,refresh=False):
  parsed=urlparse(url)
  if parsed.scheme!='https' or parsed.hostname not in {'api.weather.gov','gateway.polymarket.us'}:
   raise ValueError('Unexpected public weather host')
  started=self.clock();data=self.http.json(url,headers={'Cache-Control':'no-cache'} if refresh else None);received=self.clock()
  with (self.root/'independent-receipts.jsonl').open('a',encoding='utf-8') as f:
   f.write(json.dumps({'url':url,'request_started':started,'received':received,'cache_refresh_requested':refresh,'data':data},allow_nan=False)+'\n')
  return data,received
 def inventory(self):
  if self._markets is None or not 0<=self.clock()-self._markets[1]<30:
   raw,received=self.fetch('https://gateway.polymarket.us/v1/markets?categories=climate&active=true&closed=false&limit=500')
   rows=raw['markets']
   if not isinstance(rows,list) or len(rows)>=500 or raw.get('nextCursor'):
    raise ValueError('Complete climate inventory required')
   if len({r['slug'] for r in rows})!=len(rows):raise ValueError('Duplicate market identities')
   self._markets=(rows,received)
  return self._markets
 def forecast(self,station,target):
  if station not in STATIONS:raise ValueError('Unsupported station')
  key=(station,target);cached=self._forecasts.get(key)
  if cached and 0<=self.clock()-cached['received']<240:return cached
  location,_=self.fetch('https://api.weather.gov/stations/'+station)
  if location['properties']['stationIdentifier']!=station:raise ValueError('Station response mismatch')
  lon,lat=location['geometry']['coordinates'][:2]
  if not -180<=lon<=180 or not -90<=lat<=90:raise ValueError('Invalid station coordinates')
  point,_=self.fetch(f'https://api.weather.gov/points/{lat:.4f},{lon:.4f}')
  url=point['properties']['forecastHourly']
  if urlparse(url).hostname!='api.weather.gov':raise ValueError('Wrong forecast host')
  payload,received=self.fetch(url)
  from .forecast_journal import stamp
  revision=stamp(payload['properties']['updateTime']).timestamp()
  if received-revision>21600 and self.clock()-self._forecast_refreshes.get(station,0)>240:
   self._forecast_refreshes[station]=self.clock()
   refreshed,refreshed_at=self.fetch(url,refresh=True)
   if stamp(refreshed['properties']['updateTime']).timestamp()>=revision:
    payload,received=refreshed,refreshed_at
  result={'station':station,'received':received,'payload':payload,'source':url}
  self._forecasts[key]=result
  return result
