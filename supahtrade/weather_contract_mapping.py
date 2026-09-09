"""Explicit temperature contract identities; never infer ranges from venue slugs."""
from datetime import date
import hashlib
import re
from .nws_cli import STATIONS

def us_contract(market):
    text=market.get('description','')
    station=re.findall(r'\bK[A-Z]{3}\b',text)
    station=set(station)&set(STATIONS)
    day=re.search(r'for (\d{4}-\d{2}-\d{2})',text)
    if len(station)!=1 or not day or 'highest temperature' not in text.lower():raise ValueError('Ambiguous weather identity')
    date.fromisoformat(day[1])
    if "National Weather Service" not in text or 'Climatological Report' not in text:raise ValueError('Unverified settlement source')
    between=re.search(r'between (-?\d+)F and (-?\d+)F',text)
    below=re.search(r'(?:below|less than) (-?\d+)F',text)
    at_most=re.search(r'less than or equal to (-?\d+)F',text)
    above=re.search(r'(?:at least|greater than or equal to) (-?\d+)F',text)
    if between:
        lower,upper=map(int,between.groups())
        if lower>upper:raise ValueError('Inverted bracket')
    elif at_most:lower,upper=None,int(at_most[1])
    elif below:lower,upper=None,int(below[1])-1
    elif above:lower,upper=int(above[1]),None
    else:raise ValueError('Unsupported textual bracket; manual rule review required')
    return dict(station=station.pop(),date=day[1],lower_f=lower,upper_f=upper,source='NWS_CLI',
                slug=market['slug'],rules_sha256=hashlib.sha256(text.encode()).hexdigest())

def match_reference(reference,markets):
    """Reference must already be resolved from original contract rules.

    Different settlement sources are exposed; matching temperature bins alone
    does not establish identical outcomes or automatically authorize a trade.
    """
    keys=('station','date','lower_f','upper_f')
    if any(k not in reference for k in (*keys,'source')):raise ValueError('Incomplete reference contract')
    matches=[]
    for market in markets:
        try: candidate=us_contract(market)
        except ValueError:continue
        if all(candidate[k]==reference[k] for k in keys):
            matches.append({**candidate,'settlement_source_matches':candidate['source']==reference['source']})
    return dict(status='matched' if len(matches)==1 else 'unmatched' if not matches else 'ambiguous',
                candidates=matches,execution_eligible=len(matches)==1 and matches[0]['settlement_source_matches'])
