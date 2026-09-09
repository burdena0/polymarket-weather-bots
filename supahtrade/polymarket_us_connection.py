"""Local encrypted Polymarket US credentials, verified using account reads only."""
import json
from pathlib import Path
import threading
import time
from .local_connection import protect
from .polymarket_us import Client,decimal

class USConnection:
 def __init__(self,root,client_factory=Client):
  self.path=Path(root)/'polymarket-us.dpapi';self.factory=client_factory;self.lock=threading.RLock()
 def status(self):
  return {'status':'credentials_saved' if self.path.exists() else 'not_connected','orders_enabled':False}
 def verify(self,key_id,secret):
  with self.lock:
   client=self.factory(key_id,secret)
   balances=client.request('GET','/v1/account/balances')['balances']
   if not isinstance(balances,list):raise ValueError('Invalid balances')
   usd=[b for b in balances if b.get('currency')=='USD']
   if len(usd)!=1:raise ValueError('USD account not verified')
   decimal(usd[0]['buyingPower'])
   # No account numbers, raw balances or secrets returned to status endpoints.
   data=protect(json.dumps({'key_id':key_id,'secret':secret}).encode())
   self.path.parent.mkdir(parents=True,exist_ok=True);tmp=self.path.with_suffix('.tmp');tmp.write_bytes(data);tmp.replace(self.path)
   return {'status':'connected_read_verified','orders_enabled':False}
 def client(self,allow_orders=False):
  with self.lock:
   data=json.loads(protect(self.path.read_bytes(),True))
   return self.factory(data['key_id'],data['secret'],allow_orders=allow_orders)
 def view(self):
  if not self.path.exists():return self.status()
  try:
   client=self.client()
   balances=client.request('GET','/v1/account/balances')['balances']
   usd=[b for b in balances if b.get('currency')=='USD']
   if len(usd)!=1:raise ValueError('USD account not verified')
   result={'status':'connected_read_verified','orders_enabled':False,'received':time.time(),
           'buying_power':str(decimal(usd[0]['buyingPower'])),'currency':'USD'}
   if 'currentBalance' in usd[0]:result['current_balance']=str(decimal(usd[0]['currentBalance']))
  except Exception:
   return {'status':'account_read_failed','orders_enabled':False,'received':time.time()}
  try:
   positions=client.request('GET','/v1/portfolio/positions')
   if not isinstance(positions['positions'],dict):raise ValueError('Invalid positions')
   result['positions_count']=len(positions['positions'])
   result['positions_complete']=positions.get('eof') is True and not positions.get('nextCursor')
  except Exception:result['positions_status']='unavailable'
  return result
