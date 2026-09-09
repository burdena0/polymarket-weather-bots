"""Budget-constrained sizing against fresh order-book depth."""
from decimal import Decimal, ROUND_FLOOR
from .polymarket_us import decimal


def sized_entry(book, slug, outcome, now, minimum, budget, allowance='.04'):
    from .weather_router import depth_price
    minimum=decimal(minimum);budget=decimal(budget);allowance=decimal(allowance)
    minimum=max(Decimal('.01'),minimum)
    minimum=(minimum*100).to_integral_value(rounding='ROUND_CEILING')/100
    if minimum>5:raise ValueError('Market minimum exceeds five-share sizing limit')
    # Find the largest hundredth-share quantity (up to five) whose whole
    # order fits at its worst executable level, including fee allowance.
    low=int(minimum*100);high=500;best=None
    while low<=high:
        mid=(low+high)//2;q=Decimal(mid)/100
        try:
            px=depth_price(book,slug,outcome,'BUY',q,now)
        except ValueError as exc:
            if str(exc)!='Insufficient depth':raise
            high=mid-1;continue
        cap=q*(px+allowance)
        if cap<=budget:
            best=(q,px,cap);low=mid+1
        else:high=mid-1
    if best is None:raise ValueError('Available budget or depth below market minimum')
    return best
