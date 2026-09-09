"""Read-only forecast-change review of held independent positions. No orders."""
from decimal import Decimal
from .intraday_weather import day_bounds, summarize_intraday, probability_range, MODEL
from .weather_router import depth_price


def review_position(position, sources, clock, baseline=None):
    result={'slug':position['slug'],'outcome':position['outcome'],'reviewed_at':clock(),
            'automatic_exit':False,'model_calibrated':False,'alerts':[]}
    try:
        start,end=day_bounds(position['station'],position['date'])
        if clock()>=end:
            return {**result,'status':'awaiting_settlement','reason':'Climate day ended; forecast review cannot establish settlement.'}
        forecast=sources.forecast(position['station'],position['date'])
        observations=sources.observations(position['station'],position['date']) if clock()>=start else None
        summary=summarize_intraday(forecast,observations,position,clock())
        low,high=probability_range(summary,position,position['outcome'])
        result.update(probability_low=low,probability_high=high,
                      forecast_max_f=summary['forecast_max_f'],observed_max_f=summary['observed_max_f'],
                      forecast_revision=summary['source_update_time'],evidence_hash=summary['temperature_vector_sha256'],
                      baseline_available=False)
        # Different models must not be represented as forecast-only changes.
        if baseline and baseline.get('forecast_summary',{}).get('model')==MODEL and baseline.get('selected',{}).get('outcome')==position['outcome']:
            prior=baseline['selected'];old=baseline['forecast_summary']
            delta=low-prior['probability_low'];shift=summary['forecast_max_f']-old['forecast_max_f']
            result.update(baseline_available=True,entry_probability_low=prior['probability_low'],
                          probability_change=delta,forecast_change_f=shift)
            if delta<=-.10:result['alerts'].append('Lower probability estimate fell at least 10 percentage points from entry')
            if abs(shift)>=2:result['alerts'].append('Remaining forecast high changed at least 2F from entry; elapsed time can also cause this change')
        else:result['baseline_reason']='No matching intraday entry baseline; current estimate only'
        # Quote failure does not erase a valid forecast review.
        try:
            book=sources.book(position['slug'])
            bid=depth_price(book,position['slug'],position['outcome'],'SELL',Decimal(position['quantity']),clock())
            result['executable_bid']=str(bid)
            result['bid_note']='Depth estimate for held quantity; fees excluded, no sale submitted'
        except Exception as exc:
            result['quote_error']=str(exc)[:160] if isinstance(exc,ValueError) else type(exc).__name__
        result['status']='review_required' if result['alerts'] else 'reviewed'
    except Exception as exc:
        result.update(status='review_unavailable',reason=str(exc)[:160] if isinstance(exc,ValueError) else type(exc).__name__)
    result['reviewed_at']=clock()
    return result
