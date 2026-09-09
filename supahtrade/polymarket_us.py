"""Polymarket US authenticated transport and limit-order adapter.

No credentials are loaded automatically. Construct explicitly; mutations require
allow_orders=True. No POST retries: a lost response has an unknown outcome.
"""
import base64
from decimal import Decimal
import json
import re
import time
from urllib.request import Request, build_opener
from urllib.error import HTTPError
from urllib.parse import urlencode
from .robinhood_read import NoRedirect

HOST='https://api.polymarket.us'
class VenueError(Exception):
    def __init__(self,message,http_status=None):
        super().__init__(message)
        self.http_status=http_status

def decimal(value):
    if isinstance(value,bool): raise ValueError('Boolean is not a number')
    n=Decimal(str(value))
    if not n.is_finite() or n<0: raise ValueError('Invalid nonnegative number')
    return n

def identifier(value):
    if not isinstance(value,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}',value):
        raise ValueError('Invalid venue identifier')
    return value

def headers(key_id,secret,path,method,now):
    identifier(key_id)
    seed=base64.b64decode(secret,validate=True)
    if len(seed) not in (32,64): raise ValueError('Expected Ed25519 key')
    from nacl.signing import SigningKey
    stamp=str(int(now*1000))
    signature=SigningKey(seed[:32]).sign(f'{stamp}{method}{path}'.encode()).signature
    return {'X-PM-Access-Key':key_id,'X-PM-Timestamp':stamp,
            'X-PM-Signature':base64.b64encode(signature).decode(),'Content-Type':'application/json'}

class Client:
    def __init__(self,key_id,secret,*,allow_orders=False,transport=None,clock=time.time):
        self.key_id=key_id;self.secret=secret;self.allow_orders=allow_orders
        self.transport=transport;self.clock=clock
    def request(self,method,path,body=None,*,query=None):
        read=method=='GET' and (path in ('/v1/portfolio/activities','/v1/portfolio/positions','/v1/account/balances','/v1/orders/open') or re.fullmatch(r'/v1/order/[A-Za-z0-9_-]{1,200}',path))
        preview=method=='POST' and path=='/v1/order/preview'
        mutation=method=='POST' and (path=='/v1/orders' or re.fullmatch(r'/v1/order/[A-Za-z0-9_-]{1,200}/cancel',path))
        if not (read or preview or mutation): raise ValueError('Endpoint not allowed')
        if mutation and not self.allow_orders: raise ValueError('Order sending disabled')
        target=path
        if query is not None:
            if method!='GET' or path!='/v1/portfolio/activities' or not isinstance(query,dict):
                raise ValueError('Query not allowed')
            if set(query)-{'limit','cursor','marketSlug'}: raise ValueError('Query field not allowed')
            if 'limit' in query and (type(query['limit']) is not int or not 1<=query['limit']<=100):
                raise ValueError('Invalid page limit')
            if 'marketSlug' in query: identifier(query['marketSlug'])
            if 'cursor' in query and (not isinstance(query['cursor'],str) or not 0<len(query['cursor'])<=4096):
                raise ValueError('Invalid cursor')
            if query: target=path+'?'+urlencode(query)
        # Official SDK signs the pathname, excluding query parameters.
        h=headers(self.key_id,self.secret,path,method,self.clock())
        payload=None if body is None else json.dumps(body,allow_nan=False,separators=(',',':')).encode()
        try:
            if self.transport: return self.transport(method,target,h,payload)
            with build_opener(NoRedirect()).open(Request(HOST+target,data=payload,headers=h,method=method),timeout=8) as response:
                if response.geturl()!=HOST+target: raise VenueError('Redirect refused')
                raw=response.read(1000001)
            if len(raw)>1000000: raise VenueError('Oversized response')
            return json.loads(raw)
        except HTTPError as exc:
            # Keep the status, never the response body, URL or signed headers.
            raise VenueError('Venue HTTP request failed; mutation outcome may be unknown',exc.code) from None
        except Exception:
            raise VenueError('Venue request failed; mutation outcome may be unknown') from None
    def preview(self,order): return self.request('POST','/v1/order/preview',{'request':order})
    def create(self,order): return self.request('POST','/v1/orders',order)
    def order(self,order_id): return self.request('GET','/v1/order/'+identifier(order_id))
    def cancel(self,order_id,slug): return self.request('POST','/v1/order/'+identifier(order_id)+'/cancel',{'marketSlug':identifier(slug)})

def limit_order(slug,outcome,action,quantity,price):
    identifier(slug)
    if not slug.startswith('tc-temp-'): raise ValueError('Weather contracts only')
    if outcome not in ('YES','NO') or action not in ('BUY','SELL'): raise ValueError('Invalid order direction')
    qty=decimal(quantity);px=decimal(price)
    if not Decimal('.01')<=px<=Decimal('.99') or not 0<qty<=1000: raise ValueError('Invalid quantity or price')
    # The REST schema uses a JSON number. Keep arithmetic decimal and require
    # the serialized number to round-trip without changing the requested shares.
    wire_qty=int(qty) if qty==qty.to_integral_value() else float(qty)
    if Decimal(str(wire_qty))!=qty: raise ValueError('Quantity exceeds JSON number precision')
    wire_price=Decimal(1)-px if outcome=='NO' else px
    return dict(marketSlug=slug,type='ORDER_TYPE_LIMIT',price={'value':str(wire_price),'currency':'USD'},
                quantity=wire_qty,tif='TIME_IN_FORCE_IMMEDIATE_OR_CANCEL',
                intent='ORDER_INTENT_'+action+'_'+('LONG' if outcome=='YES' else 'SHORT'),
                manualOrderIndicator='MANUAL_ORDER_INDICATOR_AUTOMATIC')
