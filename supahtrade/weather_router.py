"""Weather reference signal -> validated US IOC order -> durable execution.

The caller owns polling and supplies timestamped source envelopes. No network
calls happen here except through an explicitly armed execution adapter.
"""
from datetime import datetime
from decimal import Decimal
import hashlib,json,re
from .reference_activity_capture import WALLET
from .weather_contract_mapping import match_reference
from .weather_execution import money
from .polymarket_us import decimal,limit_order


def depth_price(envelope,slug,outcome,action,quantity,now):
    if outcome not in ('YES','NO') or action not in ('BUY','SELL') or decimal(quantity)<=0:
        raise ValueError('Invalid order side or quantity')
    if not 0<=now-envelope['request_started']<=2 or not envelope['request_started']<=envelope['received']<=now:
        raise ValueError('Stale or invalid book receipt')
    book=envelope['data']['marketData']
    # transactTime is the last book change, not necessarily response freshness.
    # Quiet but freshly fetched books are evaluated using executable price loss.
    stamp=datetime.fromisoformat(re.sub(r'(\.\d{6})\d+',r'\1',book['transactTime']).replace('Z','+00:00'))
    if stamp.tzinfo is None or stamp.timestamp()>now:raise ValueError('Invalid book source timestamp')
    if book['marketSlug']!=slug or book['state']!='MARKET_STATE_OPEN':raise ValueError('Wrong or closed book')
    bids=[(money(r['px']),decimal(r['qty'])) for r in book['bids']]
    asks=[(money(r['px']),decimal(r['qty'])) for r in book['offers']]
    if any(not 0<p<1 or q<=0 for p,q in bids+asks):raise ValueError('Invalid level')
    if bids and asks and max(p for p,q in bids)>=min(p for p,q in asks):raise ValueError('Crossed book')
    if outcome=='NO':bids,asks=[(1-p,q) for p,q in asks],[(1-p,q) for p,q in bids]
    levels=asks if action=='BUY' else bids
    if not levels:raise ValueError('No liquidity on requested order side')
    remaining=decimal(quantity)
    for price,size in sorted(levels,reverse=action=='SELL'):
        remaining-=size
        if remaining<=0:return price
    raise ValueError('Insufficient depth')

class WeatherRouter:
    def __init__(self,execution,started_at,*,allow_cli_basis=False):
        self.execution=execution;self.started_at=started_at;self.allow_cli_basis=allow_cli_basis
    def route(self,signal,reference,markets,book,now,metadata_received):
        if signal.get('proxyWallet','').lower()!=WALLET or signal.get('type')!='TRADE':raise ValueError('Wrong reference wallet or event')
        if not self.started_at<=signal['timestamp']<=now or now-signal['timestamp']>120:raise ValueError('Expired reference signal')
        if not 0<=now-metadata_received<=60:raise ValueError('Stale rules metadata')
        # Canonical rules must remain tied to the same reference condition/token.
        if reference.get('condition_id')!=signal.get('conditionId') or reference.get('token')!=signal.get('asset'):
            raise ValueError('Reference instrument mismatch')
        outcome=signal['outcome'].upper();action=signal['side']
        if outcome not in ('YES','NO') or action not in ('BUY','SELL'):raise ValueError('Invalid signal direction')
        match=match_reference(reference,markets)
        if match['status']!='matched':return dict(status=match['status'],order=None)
        contract=match['candidates'][0]
        basis=not contract['settlement_source_matches']
        if basis and not self.allow_cli_basis:return dict(status='settlement_source_difference',order=None)
        market=next(m for m in markets if m['slug']==contract['slug'])
        if market.get('active') is not True or market.get('closed') is not False:raise ValueError('Market not active')
        tick=decimal(market['orderPriceMinTickSize']);minimum=decimal(market['minimumTradeQty'])
        if tick<=0 or minimum<=0:raise ValueError('Invalid market increments')
        quantity=Decimal(5)
        if action=='BUY':
            positions=self.execution.portfolio()['positions']
            if any(p['slug']==contract['slug'] for p in positions):raise ValueError('Bot already holds this market')
            if not hasattr(self.execution,'entry_budget') and len(positions)>=2:raise ValueError('Two-position limit')
        if action=='SELL':
            intent='ORDER_INTENT_SELL_'+('LONG' if outcome=='YES' else 'SHORT')
            available=min(quantity,self.execution.available(contract['slug'],intent))
            # Current REST previews normalize shares to hundredths. Round only
            # the exit request down; the journal retains the unsold remainder.
            quantity=available.quantize(Decimal('.01'),rounding='ROUND_FLOOR')
            if available>0 and quantity<minimum:
                return dict(status='below_market_minimum',order=None,quantity=str(available),minimum=str(max(minimum,Decimal('.01'))))
            if quantity<=0:return dict(status='no_bot_position',order=None)
            if quantity<minimum:return dict(status='below_market_minimum',order=None,quantity=str(quantity),minimum=str(minimum))
        if action=='BUY' and hasattr(self.execution,'entry_budget'):
            from .weather_sizing import sized_entry
            quantity,px,sized_cap=sized_entry(book,contract['slug'],outcome,now,minimum,self.execution.entry_budget())
        else:px=depth_price(book,contract['slug'],outcome,action,quantity,now)
        reference_price=decimal(signal['price'])
        adverse_move=px-reference_price if action=='BUY' else reference_price-px
        if adverse_move>Decimal('.02'):raise ValueError('Executable price lost more than 2 cents per share')
        order=limit_order(contract['slug'],outcome,action,quantity,px)
        if money(order['price'])%tick or quantity<minimum:raise ValueError('Price increment or minimum quantity mismatch')
        result=dict(status='ready_unarmed',order=order,reference_wallet=WALLET,cli_basis_risk=basis,
                    rules_sha256=contract['rules_sha256'],adverse_price_move=str(adverse_move),
                    book_policy='fresh_response_price_loss_2c',source_book_timestamp=book['data']['marketData']['transactTime'])
        if not self.execution.client.allow_orders:return result
        if hasattr(self.execution,'set_contract'):self.execution.set_contract(contract)
        ident=hashlib.sha256(json.dumps(signal,sort_keys=True).encode()).hexdigest()
        cap=sized_cap if action=='BUY' and hasattr(self.execution,'entry_budget') else min(Decimal(5),quantity*px+Decimal('.10'))
        result['execution']=self.execution.submit_entry(ident,order,str(cap)) if action=='BUY' else self.execution.submit_exit(ident,order,'.10')
        result['status']=result['execution']['state']
        return result
