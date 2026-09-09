"""Independent public-data weather experiment. No credentials or order submission."""
import argparse,json,math,sqlite3,time,re
from datetime import datetime
from pathlib import Path
from statistics import NormalDist
from urllib.parse import urlparse
from .providers import Http
from .forecast_journal import summarize,stamp
from .weather_contract_mapping import us_contract
from .weather_router import depth_price
from .weather_us_sources import WeatherSources
from .store import process_lock

ROOT=Path(__file__).resolve().parents[1]

def entry_criteria(row):
    """Explain the experimental decision; sensitivity is not calibrated probability."""
    reasons=[]
    fields=('price','probability_low','probability_high','assumed_cost','robust_edge')
    if any(isinstance(row.get(k),bool) or not isinstance(row.get(k),(int,float)) or not math.isfinite(row[k]) for k in fields):
        return ['invalid_candidate_numbers']
    if not 0<row['price']<1 or not 0<=row['probability_low']<=row['probability_high']<=1:
        reasons.append('invalid_probability_or_price')
    if not math.isclose(row['assumed_cost'],row['price']+.04,abs_tol=1e-9):
        reasons.append('cost_allowance_mismatch')
    if not math.isclose(row['robust_edge'],row['probability_low']-row['assumed_cost'],abs_tol=1e-9):
        reasons.append('edge_calculation_mismatch')
    if row['robust_edge']<.05:reasons.append('edge_below_five_cents_after_assumed_costs')
    return reasons

def probability_range(peak,contract,outcome):
    lo=-math.inf if contract['lower_f'] is None else contract['lower_f']-.5
    hi=math.inf if contract['upper_f'] is None else contract['upper_f']+.5
    values=[]
    # Sensitivity assumptions, not a fitted confidence interval.
    for bias in (-1,0,1):
        for sigma in (2,4):
            d=NormalDist(peak+bias,sigma);p=d.cdf(hi)-d.cdf(lo)
            values.append(p if outcome=='YES' else 1-p)
    return min(values),max(values)

class Scanner:
    def __init__(self,root):
        self.root=Path(root);self.root.mkdir(parents=True,exist_ok=True)
        self.sources=WeatherSources(self.root/'sources');self.http=Http(timeout=8,attempts=1)
        self.db=sqlite3.connect(self.root/'paper.sqlite')
        self.db.execute('CREATE TABLE IF NOT EXISTS positions(slug TEXT PRIMARY KEY, station_day TEXT UNIQUE, outcome TEXT, cost REAL, payout REAL, evidence TEXT)')
    def fetch(self,url):
        if urlparse(url).hostname not in ('api.weather.gov','gateway.polymarket.us'):raise ValueError('Unexpected public host')
        data=self.http.json(url)
        with (self.root/'receipts.jsonl').open('a',encoding='utf-8') as f:f.write(json.dumps({'received':time.time(),'url':url,'data':data})+'\n')
        return data
    def forecast(self,station,target):
        location=self.fetch('https://api.weather.gov/stations/'+station)
        if location['properties']['stationIdentifier']!=station:raise ValueError('Wrong forecast station')
        lon,lat=location['geometry']['coordinates'][:2]
        point=self.fetch(f'https://api.weather.gov/points/{lat:.4f},{lon:.4f}')
        raw=self.fetch(point['properties']['forecastHourly']);now=time.time()
        result=summarize(raw,target,now)
        if not result['complete_24_hour_day']:raise ValueError('Incomplete forecast day; intraday observations required')
        if not 0<=now-stamp(result['source_update_time']).timestamp()<=21600:raise ValueError('Forecast older than six hours')
        if stamp(result['hours'][0]['start']).timestamp()<=now:raise ValueError('Only full future days supported')
        return result
    def account(self):
        rows=self.db.execute('SELECT slug,outcome,cost,payout FROM positions').fetchall()
        cash=50-sum(r[2] for r in rows)+sum(r[3] for r in rows if r[3] is not None)
        return dict(cash=round(cash,4),realized_pnl=round(sum(r[3]-r[2] for r in rows if r[3] is not None),4),
                    positions=[dict(slug=r[0],outcome=r[1],cost=r[2],payout=r[3]) for r in rows],
                    cumulative_debits=round(sum(r[2] for r in rows),4))
    def enter(self,row):
        # One hypothetical share, one bracket/outcome per station-day, no recycling.
        if entry_criteria(row):return False
        cost=row['assumed_cost'];a=self.account()
        if row['robust_edge']<.05 or a['cash']-cost<40 or a['cumulative_debits']+cost>10:return False
        with self.db:
            return bool(self.db.execute('INSERT OR IGNORE INTO positions VALUES(?,?,?,?,NULL,?)',
                (row['slug'],row['station']+row['date'],row['outcome'],cost,json.dumps(row))).rowcount)
    def cycle(self):
        report=dict(at=time.time(),mode='paper_only',model='NWS hourly maximum; uncalibrated normal sensitivity',
                    assumptions='sigma 2/4 F, bias -1/0/+1 F; 3 cents/share fee allowance plus 1 cent slippage. Snapshot fills are hypothetical.',candidates=[],errors=[])
        try:
            for slug,outcome in self.db.execute('SELECT slug,outcome FROM positions WHERE payout IS NULL').fetchall():
                try:
                    raw,_=self.sources.settlement(slug)
                    value=float(raw['settlement'])
                    if raw['slug']!=slug or not math.isfinite(value) or not 0<=value<=1:raise ValueError('Invalid settlement')
                    with self.db:self.db.execute('UPDATE positions SET payout=? WHERE slug=? AND payout IS NULL',(value if outcome=='YES' else 1-value,slug))
                except Exception:pass # Unresolved is never assumed to be a loss or win.
            markets=self.fetch('https://gateway.polymarket.us/v1/markets?categories=climate&active=true&closed=false&limit=500')['markets']
            if len(markets)>=500:raise ValueError('Inventory pagination required')
            forecasts={}
            for market in markets:
                try:
                    if market.get('closed') or not market.get('active'):continue
                    contract=us_contract(market);key=(contract['station'],contract['date'])
                    if key not in forecasts:
                        try:forecasts[key]=self.forecast(*key)
                        except Exception as e:forecasts[key]=str(e)
                    forecast=forecasts[key]
                    if isinstance(forecast,str):raise ValueError(forecast)
                    book=self.sources.book(contract['slug'])
                    # Python 3.10 needs subsecond precision truncated to microseconds.
                    book['data']['marketData']['transactTime']=re.sub(r'(\.\d{6})\d+',r'\1',book['data']['marketData']['transactTime'])
                    for outcome in ('YES','NO'):
                        # One unavailable side must not suppress the opposite side.
                        try:
                            price=float(depth_price(book,contract['slug'],outcome,'BUY',1,time.time()))
                            low,high=probability_range(forecast['forecast_max_f'],contract,outcome)
                            row=dict(**contract,outcome=outcome,price=price,probability_low=low,probability_high=high,
                                     forecast_max_f=forecast['forecast_max_f'],forecast_received=forecast['known_available_to_bot_by'],
                                     assumed_cost=price+.04,robust_edge=low-price-.04,
                                     signal_origin='independent_nws_forecast',requires_reference_trade=False,
                                     model_calibrated=False,execution_eligible=False)
                            row['rejection_reasons']=entry_criteria(row)
                            report['candidates'].append(row)
                        except Exception as e:
                            report['errors'].append({'slug':contract['slug'],'outcome':outcome,'reason':str(e)[:160]})
                except Exception as e:report['errors'].append({'slug':market.get('slug'),'reason':str(e)[:160]})
            for row in sorted(report['candidates'],key=lambda r:r['robust_edge'],reverse=True):row['new_paper_entry']=self.enter(row)
        except Exception as e:report['error']=str(e)[:160]
        report['account']=self.account()
        for name,path in [('copier_live','data/us-weather-live-v1/status.json'),('copier_observer','data/us-weather-cli-observer-v2/status.json')]:
            try:
                s=json.loads((ROOT/path).read_text());report[name]={k:s.get(k) for k in ('at','status','armed','portfolio')}
            except Exception:report[name]={'status':'unavailable'}
        with (self.root/'cycles.jsonl').open('a') as f:f.write(json.dumps(report)+'\n')
        tmp=self.root/'status.tmp';tmp.write_text(json.dumps(report));tmp.replace(self.root/'status.json')
        return report

def main():
    p=argparse.ArgumentParser();p.add_argument('--once',action='store_true');args=p.parse_args()
    root=ROOT/'data/independent-weather-paper-v2'
    with process_lock(root/'scanner'):
        s=Scanner(root);deadline=stamp(json.loads((ROOT/'routines/schedule.json').read_text())['stop_after']).timestamp()
        try:
            while time.time()<deadline and not (root/'STOP').exists():
                r=s.cycle();print(json.dumps({'candidates':len(r['candidates']),'rejections':len(r['errors']),'error':r.get('error')}),flush=True)
                if args.once:break
                for _ in range(300):
                    if (root/'STOP').exists() or time.time()>=deadline:break
                    time.sleep(1)
        finally:s.db.close()
if __name__=='__main__':main()
