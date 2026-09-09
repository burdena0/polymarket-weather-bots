"""Prospective forecast receipt journal. Source update time is not publication time."""
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from .providers import Http

SOURCE='https://api.weather.gov/gridpoints/OKX/37,46/forecast/hourly'
TARGET='2026-09-09'


def stamp(text):
    value=datetime.fromisoformat(text.replace('Z','+00:00'))
    if value.tzinfo is None: raise ValueError('Forecast timestamp needs timezone')
    return value


def summarize(payload,target,received):
    props=payload['properties']; updated=stamp(props['updateTime'])
    if updated.timestamp()>received: raise ValueError('Source update is in the future')
    hours=[]
    for period in props['periods']:
        start,end=stamp(period['startTime']),stamp(period['endTime'])
        if start.date().isoformat()!=target: continue
        temp=period['temperature']
        if isinstance(temp,bool) or not isinstance(temp,(int,float)) or not -150<=temp<=150:
            raise ValueError('Invalid temperature')
        if period['temperatureUnit']!='F' or (end-start).total_seconds()!=3600:
            raise ValueError('Expected hourly Fahrenheit periods')
        hours.append({'start':start.isoformat(),'temperature_f':temp})
    hours.sort(key=lambda h:stamp(h['start']))
    starts=[stamp(h['start']) for h in hours]
    complete=(len(starts)==24 and {s.hour for s in starts}==set(range(24)) and
              all((b-a).total_seconds()==3600 for a,b in zip(starts,starts[1:])))
    vector=json.dumps(hours,sort_keys=True,allow_nan=False)
    return {'target_date':target,'source_update_time':updated.isoformat(),'hours':hours,
            'temperature_vector_sha256':hashlib.sha256(vector.encode()).hexdigest(),
            'complete_24_hour_day':complete,
            'forecast_max_f':max(h['temperature_f'] for h in hours) if complete else None,
            'publication_time':None,'known_available_to_bot_by':received,
            'calibrated_probability':None,'execution_eligible':False}


def connect(path):
    Path(path).parent.mkdir(parents=True,exist_ok=True)
    db=sqlite3.connect(path)
    db.execute('''CREATE TABLE IF NOT EXISTS receipts(id INTEGER PRIMARY KEY,
        received REAL NOT NULL, request_started REAL NOT NULL, source TEXT NOT NULL,
        raw_sha TEXT NOT NULL, raw TEXT NOT NULL, summary TEXT NOT NULL)''')
    return db


def record(path,payload,started,received,target=TARGET):
    if not 0<started<=received or received-started>180:
        raise ValueError('Invalid request timing')
    summary=summarize(payload,target,received)
    raw=json.dumps(payload,sort_keys=True,allow_nan=False)
    db=connect(path)
    try:
        with db:
            db.execute('BEGIN IMMEDIATE')
            previous=db.execute('SELECT received,summary FROM receipts ORDER BY id DESC LIMIT 1').fetchone()
            if previous and received<previous[0]: raise ValueError('Receipt clock moved backwards')
            prev=json.loads(previous[1]) if previous else None
            summary['temperature_vector_changed']=(summary['temperature_vector_sha256']!=prev['temperature_vector_sha256']) if prev and prev['target_date']==target else None
            summary['source_revision_changed']=(summary['source_update_time']!=prev['source_update_time']) if prev else None
            summary['first_observation']=previous is None
            ident=db.execute('INSERT INTO receipts(received,request_started,source,raw_sha,raw,summary) VALUES(?,?,?,?,?,?)',
                (received,started,SOURCE,hashlib.sha256(raw.encode()).hexdigest(),raw,json.dumps(summary))).lastrowid
        return {**summary,'receipt_id':ident,'source':SOURCE,'raw_sha256':hashlib.sha256(raw.encode()).hexdigest()}
    finally: db.close()


def as_of(path,cutoff):
    db=sqlite3.connect(Path(path).resolve().as_uri()+'?mode=ro',uri=True)
    try:
        row=db.execute('SELECT summary FROM receipts WHERE received<=? ORDER BY received DESC,id DESC LIMIT 1',(cutoff,)).fetchone()
        return json.loads(row[0]) if row else None
    finally: db.close()


def capture(root):
    started=time.time(); payload=Http().json(SOURCE);received=time.time()
    return record(Path(root)/'data/forecast-receipts.sqlite',payload,started,received)
