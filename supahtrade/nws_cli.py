"""NWS CLI settlement-source observations; never a venue settlement receipt."""
import argparse
from datetime import datetime
import hashlib
import json
from pathlib import Path
import re
import time
from urllib.request import Request, urlopen

STATIONS = {'KLAX':'LAX','KNYC':'NYC','KSFO':'SFO','KMIA':'MIA','KMDW':'MDW'}
MONTHS = {m:i for i,m in enumerate(('JANUARY','FEBRUARY','MARCH','APRIL','MAY','JUNE','JULY','AUGUST','SEPTEMBER','OCTOBER','NOVEMBER','DECEMBER'),1)}

def parse_product(product, station, received):
    location=STATIONS[station];text=product['productText']
    if product.get('productCode')!='CLI' or not re.search(r'^CLI'+location+r'\s*$',text,re.M):
        raise ValueError('Wrong CLI product')
    issued=datetime.fromisoformat(product['issuanceTime'])
    if issued.tzinfo is None or issued.timestamp()>received+60:
        raise ValueError('Invalid issuance timestamp')
    match=re.search(r'CLIMATE SUMMARY FOR\s+([A-Z]+)\s+(\d{1,2})\s+(\d{4})',text)
    if not match: raise ValueError('Missing observation date')
    month,day,year=match.groups();date=datetime(int(year),MONTHS[month],int(day)).date().isoformat()
    section=re.search(r'TEMPERATURE\s*\(F\)(.*?)(?:PRECIPITATION|DEGREE DAYS|\Z)',text,re.S)
    maximum=re.search(r'^\s*MAXIMUM\s+(-?\d+|MM)(?:R)?\s',section.group(1),re.M) if section else None
    if not maximum: raise ValueError('Missing maximum field')
    high=None if maximum[1]=='MM' else int(maximum[1])
    if high is not None and not -100<=high<=150: raise ValueError('Implausible maximum')
    # An intraday CLI may report a partial day. Keep it visible but ineligible.
    partial=bool(re.search(r'VALID\s+(?:TODAY\s+)?AS OF',text,re.I))
    return dict(station=station,product='CLI'+location,observation_date=date,high_f=high,
                issued_at=product['issuanceTime'],received_at=received,product_id=product['id'],
                sha256=hashlib.sha256(text.encode()).hexdigest(),corrected=bool(re.search(r'CORRECTED|\bCOR\b',text)),
                partial_day=partial,status='missing' if high is None else 'partial' if partial else 'observed',
                settlement_confirmed=False,source='https://api.weather.gov/products/'+product['id'])

def get_json(path):
    request=Request('https://api.weather.gov'+path,headers={'User-Agent':'SupahTrade/1.0 (local weather research)','Accept':'application/geo+json'})
    with urlopen(request,timeout=5) as response:
        raw=response.read(1000001)
    if len(raw)>1000000: raise ValueError('Oversized NWS response')
    return json.loads(raw)

def collect(root, fetch=get_json, clock=time.time):
    root=Path(root);root.mkdir(parents=True,exist_ok=True);views=[]
    for station,location in STATIONS.items():
        try:
            listing=fetch('/products/types/CLI/locations/'+location)['@graph']
            if not listing: raise ValueError('No CLI reports')
            newest=max(listing,key=lambda x:datetime.fromisoformat(x['issuanceTime']))
            ident=newest['id']
            if not re.fullmatch(r'[a-fA-F0-9-]{36}',ident): raise ValueError('Invalid product ID')
            product=fetch('/products/'+ident)
            if product['id']!=ident: raise ValueError('Product identity mismatch')
            received=clock();view=parse_product(product,station,received)
            rawpath=root/(ident+'.json')
            if not rawpath.exists(): rawpath.write_text(json.dumps(dict(received_at=received,product=product),indent=2))
            views.append(view)
        except Exception as exc:
            views.append(dict(station=station,status='unavailable',error=type(exc).__name__,settlement_confirmed=False))
    result=dict(received_at=clock(),stations=views,coverage='Latest NWS CLI per station; observation date is explicit. Not a forecast or exchange settlement confirmation.')
    temp=root/'latest.tmp';temp.write_text(json.dumps(result,indent=2));temp.replace(root/'latest.json')
    return result

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--root',default='data/nws-cli');args=parser.parse_args()
    print(json.dumps(collect(args.root),indent=2))
