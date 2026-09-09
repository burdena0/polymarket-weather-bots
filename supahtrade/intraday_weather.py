"""Experimental CLI-day forecast/observation fusion; no network or orders."""
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from statistics import NormalDist
from .forecast_journal import stamp

OFFSETS = {'KLAX':-8,'KSFO':-8,'KMIA':-5,'KNYC':-5,'KMDW':-6}
MODEL = 'independent_nws_intraday_v2'


def day_bounds(station, target):
    # NWS CLI calendar days use local STANDARD time throughout the year.
    start=datetime.fromisoformat(target).replace(tzinfo=timezone(timedelta(hours=OFFSETS[station])))
    return start.timestamp(), (start+timedelta(days=1)).timestamp()


def summarize_intraday(forecast, observations, contract, now):
    station=contract['station'];start,end=day_bounds(station,contract['date'])
    if now>=end:raise ValueError('Climate day ended; await CLI settlement')
    if forecast['station']!=station:raise ValueError('Forecast station mismatch')
    received=forecast['received']
    if not 0<=now-received<=300:raise ValueError('Stale forecast receipt')
    props=forecast['payload']['properties'];revision=stamp(props['updateTime']).timestamp()
    if not 0<=received-revision<=21600:raise ValueError('Stale or future forecast revision')
    cutoff=max(start,start+math.floor(max(0,now-start)/3600)*3600)
    hours={}
    for period in props['periods']:
        a=stamp(period['startTime']).timestamp();b=stamp(period['endTime']).timestamp()
        if not cutoff<=a<end:continue
        value=period['temperature']
        if b-a!=3600 or (a-start)%3600 or isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not -150<=value<=150 or period['temperatureUnit']!='F':
            raise ValueError('Invalid remaining hourly forecast')
        if a in hours:raise ValueError('Duplicate forecast hour')
        hours[a]=value
    expected=set(range(int(cutoff),int(end),3600))
    if set(hours)!=expected:raise ValueError('Missing remaining forecast hours')
    observed={}
    if now>=start:
        if observations is None:raise ValueError('Same-day station observations required')
        if observations['station']!=station:raise ValueError('Observation station mismatch')
        if not 0<=now-observations['received']<=300:raise ValueError('Stale observation receipt')
        payload=observations['payload']
        if payload.get('pagination',{}).get('next'):raise ValueError('Incomplete observation pagination')
        for feature in payload['features']:
            p=feature['properties']
            if p.get('station','').rstrip('/').rsplit('/',1)[-1]!=station:raise ValueError('Observation identity mismatch')
            at=stamp(p['timestamp']).timestamp()
            if at>observations['received'] or at>now:raise ValueError('Future observation timestamp')
            if not start<=at<end:continue
            temp=p['temperature'];value=temp.get('value')
            if value is None or temp.get('qualityControl') not in ('V','S'):continue
            if temp.get('unitCode')!='wmoUnit:degC':raise ValueError('Unexpected observation unit')
            if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or not -100<=value<=65:raise ValueError('Invalid observation temperature')
            value=value*9/5+32
            if at in observed and observed[at]!=value:raise ValueError('Conflicting duplicate observation')
            observed[at]=value
        if not observed:raise ValueError('No usable same-day observations')
        if now-max(observed)>5400:raise ValueError('Latest station observation older than 90 minutes')
        covered={int((at-start)//3600) for at in observed}
        required=set(range(int((cutoff-start)//3600)))
        missing=required-covered
        if missing:raise ValueError(f'Missing observations for {len(missing)} elapsed hours')
    evidence={'hours':sorted(hours.items()),'observations':sorted(observed.items()),'day_start':start,'day_end':end}
    return {'target_date':contract['date'],'day_start':start,'day_end':end,'day_basis':'local_standard_time',
            'source_update_time':props['updateTime'],'remaining_hours':len(hours),
            'forecast_max_f':max(hours.values()),'observed_max_f':max(observed.values()) if observed else None,
            'observation_count':len(observed),'observations_received':observations['received'] if observed else None,
            'known_available_to_bot_by':received,'temperature_vector_sha256':hashlib.sha256(json.dumps(evidence,sort_keys=True).encode()).hexdigest(),
            'model':MODEL,'model_calibrated':False,
            'assumptions':'Maximum of uncertain sampled past high and remaining forecast high. Past high: normal sigma 1F with missing-peak shifts 0/2F. Future high: bias -1/0/+1F, sigma 2/4F. Independent components assumed; sensitivity range is not a confidence interval or settlement certainty.'}


def probability_range(summary,contract,outcome):
    if outcome not in ('YES','NO'):raise ValueError('Invalid outcome')
    lo=-math.inf if contract['lower_f'] is None else contract['lower_f']-.5
    hi=math.inf if contract['upper_f'] is None else contract['upper_f']+.5
    values=[]
    for bias in (-1,0,1):
        for sigma in (2,4):
            future=NormalDist(summary['forecast_max_f']+bias,sigma)
            for shift in (0,2):
                past=NormalDist(summary['observed_max_f']+shift,1) if summary['observed_max_f'] is not None else None
                def cdf(x):return future.cdf(x)*(past.cdf(x) if past else 1)
                p=max(0,min(1,cdf(hi)-cdf(lo)))
                values.append(p if outcome=='YES' else 1-p)
    return min(values),max(values)
