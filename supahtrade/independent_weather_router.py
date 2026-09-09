"""Independent NWS candidate planning. No wallet, credentials or order submission.

The probability range is an experimental sensitivity calculation, not a fitted
confidence interval. Plans retain that provenance for the execution supervisor.
"""
import hashlib
import json
from decimal import Decimal

from .forecast_journal import stamp, summarize
from .independent_weather import probability_range
from .polymarket_us import decimal, limit_order
from .weather_contract_mapping import us_contract
from .weather_execution import money
from .weather_router import depth_price


def plan_entry(market, forecast, book, now, metadata_received, positions=(), *, intraday=False, observations=None, entry_budget=None):
    """Forecast envelope: station, received and original NWS hourly payload.

    The source adapter must establish station provenance from the NWS station
    -> point -> hourly chain. This function rechecks raw hourly data, rather
    than trusting cached probability or edge fields from a previous scan.
    """
    if not 0 <= now-metadata_received <= 60:
        raise ValueError('Stale rules metadata')
    contract=us_contract(market)
    if market.get('active') is not True or market.get('closed') is not False:
        raise ValueError('Market not active')
    if forecast['station']!=contract['station']:
        raise ValueError('Forecast station mismatch')
    if not 0 <= now-forecast['received'] <= 300:
        raise ValueError('Stale forecast receipt')
    if intraday:
        from .intraday_weather import summarize_intraday, probability_range as intraday_range, MODEL
        summary=summarize_intraday(forecast,observations,contract,now)
    else:
        summary=summarize(forecast['payload'],contract['date'],forecast['received'])
        if not summary['complete_24_hour_day']:
            raise ValueError('Complete forecast day required')
        if not 0 <= now-stamp(summary['source_update_time']).timestamp() <= 21600:
            raise ValueError('Forecast revision older than six hours')
        if stamp(summary['hours'][0]['start']).timestamp() <= now:
            raise ValueError('Full future day required')
    if entry_budget is None and len(positions)>=2:
        raise ValueError('Two-position limit')
    if any(p['slug']==contract['slug'] for p in positions):
        raise ValueError('Bot already holds this market')
    # Correlated brackets share the same station and settlement day.
    for p in positions:
        if 'station' not in p or 'date' not in p:
            raise ValueError('Held contract identities required')
        if (p['station'],p['date'])==(contract['station'],contract['date']):
            raise ValueError('Station-day exposure already held')
    tick=decimal(market['orderPriceMinTickSize'])
    minimum=decimal(market['minimumTradeQty'])
    quantity=Decimal(5)
    if tick<=0 or minimum<=0 or quantity<minimum:
        raise ValueError('Invalid increments or minimum quantity')
    candidates=[]
    for outcome in ('YES','NO'):
        row={'outcome':outcome,'order':None}
        try:
            if entry_budget is not None:
                from .weather_sizing import sized_entry
                quantity,price,_=sized_entry(book,contract['slug'],outcome,now,minimum,entry_budget)
            else:
                price=depth_price(book,contract['slug'],outcome,'BUY',quantity,now)
            low,high=intraday_range(summary,contract,outcome) if intraday else probability_range(summary['forecast_max_f'],contract,outcome)
            # Conservative limit-price cost for every share, plus explicit
            # experimental fee/slippage allowance. Actual preview fees remain
            # the executor's responsibility before any submission.
            allowance=Decimal('.04')*quantity
            cap=quantity*price+allowance
            edge=Decimal(str(low))-price-Decimal('.04')
            row.update(price=str(price),probability_low=low,probability_high=high,
                       edge_after_allowance=str(edge),cost_cap=str(cap),
                       allowance_per_share='0.04',minimum_edge_per_share='0.05',
                       maximum_entry_price=str(Decimal(str(low))-Decimal('.09')),
                       required_price_improvement=str(max(Decimal(0),Decimal('.05')-edge)))
            if cap>5:raise ValueError('Five-dollar order cap')
            if edge<Decimal('.05'):raise ValueError('Edge below five cents after allowance')
            order=limit_order(contract['slug'],outcome,'BUY',quantity,price)
            if money(order['price'])%tick:raise ValueError('Price tick mismatch')
            row.update(order=order,status='candidate')
        except ValueError as exc:
            row.update(status='rejected',reason=str(exc))
        candidates.append(row)
    eligible=[r for r in candidates if r['order'] is not None]
    selected=max(eligible,key=lambda r:Decimal(r['edge_after_allowance'])) if eligible else None
    identity={'strategy':MODEL if intraday else 'independent_nws_sensitivity_v1','rules':contract['rules_sha256'],
              'slug':contract['slug'],'forecast':summary['temperature_vector_sha256'],
              'outcome':selected['outcome'] if selected else None}
    return {'status':'candidate' if selected else 'rejected','contract':contract,
            'signal_id':hashlib.sha256(json.dumps(identity,sort_keys=True).encode()).hexdigest(),
            'signal_origin':'independent_nws_forecast','requires_reference_trade':False,
            'model_calibrated':False,'orders_submitted':False,'selected':selected,
            'sides':candidates,'forecast_summary':summary}
