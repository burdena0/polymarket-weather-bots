"""Published weather taker fee arithmetic; actual match parameters still required.

Source: https://docs.polymarket.com/trading/fees
fee = shares * feeRate * price * (1-price); weather category rate 0.05.
No rounding, maker rebates, or account eligibility is inferred here.
"""
import math
import json

FEE_MODELS = ('legacy_flat_1pct', 'weather_taker_005')


def verify_weather_schedule(market, info):
    """Require agreement of captured Gamma and CLOB parameters, not match-time proof."""
    schedule=market.get('feeSchedule',{})
    details=info.get('fd',{})
    if (market.get('feesEnabled') is not True or schedule.get('rate') != .05
            or schedule.get('exponent') != 1 or schedule.get('takerOnly') is not True
            or details.get('r') != .05 or details.get('e') != 1 or details.get('to') is not True):
        raise ValueError('Unsupported or conflicting weather fee schedule')
    tokens=json.loads(market['clobTokenIds']);outcomes=json.loads(market['outcomes'])
    actual=info.get('t',[])
    if (info.get('c','').lower()!=market['conditionId'].lower() or len(tokens)!=2
            or len(set(tokens))!=2 or len(outcomes)!=2 or set(outcomes)!={'Yes','No'}
            or len(actual)!=2 or dict(zip(tokens,outcomes))!={r['t']:r['o'] for r in actual}):
        raise ValueError('Fee response instrument mismatch')
    return dict(rate=.05,exponent=1,taker_only=True,captured_sources_agree=True,actual_match_fee_verified=False)


def taker_fee(shares, price, rate=.05):
    if any(isinstance(v,bool) or not isinstance(v,(int,float)) or not math.isfinite(v) for v in (shares,price,rate)):
        raise ValueError('Finite numeric inputs required')
    if shares<0 or not 0<=price<=1 or not 0<=rate<=1:
        raise ValueError('Invalid fee inputs')
    return shares*rate*price*(1-price)


def trade_cash(shares, price, side, fee_model):
    taker_fee(shares,price)  # Validate inputs before either model.
    if side not in ('BUY','SELL') or fee_model not in FEE_MODELS:
        raise ValueError('Unknown side or fee model')
    if fee_model == 'legacy_flat_1pct':
        return shares*(price+.001)*1.01 if side=='BUY' else shares*max(0,price-.001)*.99
    # Fee evaluated at the quoted price; adverse cash adjustment is separate.
    fee=taker_fee(shares,price)
    return shares*(price+.001)+fee if side=='BUY' else max(0,shares*(price-.001)-fee)
