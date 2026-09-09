import base64,json,tempfile,unittest
from pathlib import Path
from nacl.signing import SigningKey
from supahtrade.polymarket_us import Client,headers,limit_order,VenueError
from supahtrade.weather_execution import Execution

class USExecutionTests(unittest.TestCase):
 def setUp(self):
  self.key=SigningKey.generate();self.secret=base64.b64encode(bytes(self.key)).decode();self.calls=[]
  self.order=limit_order('tc-temp-laxhigh-2026-09-09-gte89lt90f','NO','BUY',5,'.40')
  self.state='ORDER_STATE_FILLED';self.fail=False;self.qty=5
  self.tmp=tempfile.TemporaryDirectory();self.path=Path(self.tmp.name)/'orders.sqlite'
  self.client=Client('test-key',self.secret,allow_orders=True,transport=self.transport)
  self.engine=Execution(self.path,self.client)
 def tearDown(self):self.engine.close();self.tmp.cleanup()
 def transport(self,method,path,h,body):
  self.calls.append((method,path))
  if path=='/v1/order/preview':return {'order':{**self.order,'commissionNotionalTotalCollected':{'value':'.01','currency':'USD'}}}
  if path=='/v1/orders':
   if self.fail:raise TimeoutError()
   return {'id':'order-1'}
  if path.endswith('/cancel'):self.state='ORDER_STATE_CANCELED';return {}
  return {'order':{**self.order,'id':'order-1','state':self.state,'cumQuantity':self.qty,'avgPx':{'value':'.61','currency':'USD'},'commissionNotionalTotalCollected':{'value':'.01','currency':'USD'}}}
 def test_signature_protocol(self):
  h=headers('test-key',self.secret,'/v1/orders','POST',1234.5)
  self.key.verify_key.verify(b'1234500POST/v1/orders',base64.b64decode(h['X-PM-Signature']))
 def test_activity_query_is_encoded_but_not_signed(self):
  from urllib.parse import urlsplit,parse_qs
  def read(method,path,h,body):
   self.assertEqual(parse_qs(urlsplit(path).query),{'cursor':['a+/='],'limit':['100']})
   message=(h['X-PM-Timestamp']+'GET/v1/portfolio/activities').encode()
   self.key.verify_key.verify(message,base64.b64decode(h['X-PM-Signature']))
   return {'activities':[],'eof':True}
  self.client.transport=read
  self.client.request('GET','/v1/portfolio/activities',query={'cursor':'a+/=','limit':100})
 def test_query_cannot_expand_endpoint_permissions(self):
  for path,query in [('/v1/orders',{'limit':1}),('/v1/portfolio/activities',{'redirect':'x'}),('/v1/portfolio/activities',{'limit':True})]:
   with self.assertRaises(ValueError):self.client.request('GET',path,query=query)
  self.assertEqual(self.calls,[])
 def test_fill_restart_duplicate_and_actual_fee(self):
  r=self.engine.submit_entry('signal-1',self.order,'2.10');self.assertEqual(r['actual_debit'],1960000)
  self.engine.close();self.engine=Execution(self.path,self.client)
  self.engine.submit_entry('signal-1',self.order,'2.10')
  self.assertEqual(self.calls.count(('POST','/v1/orders')),1)
 def test_timeout_keeps_reserve_never_resubmits(self):
  self.fail=True;r=self.engine.submit_entry('signal-1',self.order,'2.10')
  self.assertEqual(r['state'],'unknown');self.assertEqual(self.engine.journal.reserved(),2100000)
  self.engine.submit_entry('signal-1',self.order,'2.10');self.assertEqual(self.calls.count(('POST','/v1/orders')),1)
 def test_timeout_diagnostic_is_durable_and_sanitized(self):
  self.fail=True;self.engine.submit_entry('lost',self.order,'2.10')
  row=self.engine.journal.db.execute('SELECT stage,error_type,http_status,venue_id FROM submission_diagnostics WHERE local_id=?',('lost',)).fetchone()
  self.assertEqual(tuple(row),('create_request','VenueError',None,None))
 def test_http_failure_preserves_status_without_secret(self):
  from urllib.error import HTTPError
  def failed(method,path,h,body):
   raise HTTPError('https://example.invalid/secret',503,'private response',{},None)
  self.client.transport=failed
  with self.assertRaises(VenueError) as caught:self.client.request('GET','/v1/account/balances')
  self.assertEqual(caught.exception.http_status,503)
  self.assertNotIn('private',str(caught.exception));self.assertNotIn('secret',str(caught.exception))
 def test_missing_create_id_records_stage_without_resending(self):
  original=self.transport
  def missing(method,path,h,body):
   if path=='/v1/orders':
    self.calls.append((method,path));return {'unexpected':'sensitive-body'}
   return original(method,path,h,body)
  self.client.transport=missing
  result=self.engine.submit_entry('missing',self.order,'2.10')
  self.assertEqual(result['state'],'unknown')
  row=self.engine.journal.db.execute('SELECT stage,error_type FROM submission_diagnostics WHERE local_id=?',('missing',)).fetchone()
  self.assertEqual(tuple(row),('parse_create_response','KeyError'))
  self.engine.submit_entry('missing',self.order,'2.10')
  self.assertEqual(self.calls.count(('POST','/v1/orders')),1)
 def test_operator_association_recovers_unknown_without_resending(self):
  self.fail=True;self.engine.submit_entry('lost',self.order,'2.10')
  self.client.allow_orders=False
  result=self.engine.recover_order_id('lost','order-1')
  self.assertEqual(result['state'],'settled')
  self.assertEqual(self.calls.count(('POST','/v1/orders')),1)
  self.assertEqual(self.engine.journal.unresolved(),[])
 def test_recovery_mismatched_order_leaves_unknown_reserved(self):
  self.fail=True;self.engine.submit_entry('lost',self.order,'2.10')
  self.order={**self.order,'quantity':4}
  with self.assertRaisesRegex(ValueError,'quantity or limit'):
   self.engine.recover_order_id('lost','order-1')
  self.assertEqual(self.engine.journal.get('lost')['state'],'unknown')
  self.assertEqual(self.engine.journal.reserved(),2100000)
 def test_partial_cancel_accounts_for_fills(self):
  self.state='ORDER_STATE_PARTIALLY_FILLED';self.qty=2
  self.assertEqual(self.engine.submit_entry('signal-1',self.order,'2.10')['state'],'inflight')
  self.assertEqual(self.engine.cancel('signal-1')['actual_debit'],790000)
 def test_partial_fill_survives_restart_then_cancel_without_resubmission(self):
  self.state='ORDER_STATE_PARTIALLY_FILLED';self.qty=2
  self.engine.submit_entry('restart-partial',self.order,'2.10')
  self.engine.close();self.engine=Execution(self.path,self.client)
  self.assertEqual(self.engine.portfolio()['positions'][0]['quantity'],'2')
  self.assertEqual(len(self.engine.journal.unresolved()),1)
  result=self.engine.cancel('restart-partial')
  self.assertEqual(result['actual_debit'],790000)
  self.assertEqual(self.engine.journal.unresolved(),[])
  self.engine.close();self.engine=Execution(self.path,self.client)
  self.engine.submit_entry('restart-partial',self.order,'2.10')
  self.assertEqual(self.calls.count(('POST','/v1/orders')),1)
  self.assertEqual(self.engine.portfolio()['positions'][0]['quantity'],'2')
 def test_decreasing_partial_fill_does_not_replace_inventory(self):
  self.state='ORDER_STATE_PARTIALLY_FILLED';self.qty=3
  self.engine.submit_entry('partial',self.order,'2.10')
  self.qty=2
  with self.assertRaisesRegex(ValueError,'Cumulative fills decreased'):
   self.engine.reconcile('partial')
  self.assertEqual(self.engine.portfolio()['positions'][0]['quantity'],'3')
  self.assertEqual(len(self.engine.journal.unresolved()),1)
 def test_disarmed_and_excess_cap_no_network(self):
  self.client.allow_orders=False
  with self.assertRaises(ValueError):self.engine.submit_entry('signal-1',self.order,'2.10')
  self.client.allow_orders=True
  with self.assertRaises(ValueError):self.engine.submit_entry('signal-1',self.order,'6')
  self.assertEqual(self.calls,[])
 def test_preview_rejection_releases_only_undispatched_budget(self):
  def rejected_preview(method,path,h,body):
   self.calls.append((method,path))
   self.assertEqual(path,'/v1/order/preview')
   return {'order':{**self.order,'state':'ORDER_STATE_REJECTED'}}
  self.client.transport=rejected_preview
  result=self.engine.submit_entry('rejected-preview',self.order,'2.10')
  self.assertEqual(result['state'],'rejected')
  self.assertEqual(self.engine.journal.reserved(),0)
  self.engine.submit_entry('rejected-preview',self.order,'2.10')
  self.assertEqual(len(self.calls),1)
 def test_unknown_submission_cannot_release_reservation_locally(self):
  self.fail=True;self.engine.submit_entry('unknown',self.order,'2.10')
  with self.assertRaisesRegex(ValueError,'undispatched'):
   self.engine.journal.reject_prepared('unknown')
  self.assertEqual(self.engine.journal.reserved(),2100000)
 def test_nonweather_and_path_injection_rejected(self):
  with self.assertRaises(ValueError):limit_order('sports-game','YES','BUY',1,'.5')
  with self.assertRaises(ValueError):self.client.order('../orders')
  self.assertEqual(self.calls,[])
 def test_exit_uses_owned_contracts_and_charges_only_fees(self):
  orders={};counter=[0]
  def transport(method,path,h,body):
   if path=='/v1/order/preview':
    req=json.loads(body)['request'];return {'order':{**req,'commissionNotionalTotalCollected':{'value':'.01','currency':'USD'}}}
   if path=='/v1/orders':
    counter[0]+=1;ident='order-'+str(counter[0]);req=json.loads(body)
    orders[ident]={**req,'id':ident,'state':'ORDER_STATE_FILLED','cumQuantity':req['quantity'],'avgPx':req['price'],'commissionNotionalTotalCollected':{'value':'.01','currency':'USD'}}
    return {'id':ident}
   return {'order':orders[path.rsplit('/',1)[-1]]}
  self.client.transport=transport
  self.engine.submit_entry('buy',self.order,'2.10')
  sell=limit_order(self.order['marketSlug'],'NO','SELL',5,'.6')
  result=self.engine.submit_exit('sell',sell,'.10')
  self.assertEqual(result['actual_debit'],10000)
  self.assertEqual(self.engine.available(sell['marketSlug'],sell['intent']),0)
  with self.assertRaises(ValueError):self.engine.submit_exit('oversell',sell,'.10')
  self.assertEqual(counter[0],2)
  self.assertEqual(self.engine.portfolio()['realized_pnl'],'0.98')
  self.assertEqual(self.engine.portfolio()['positions'],[])
 def test_terminal_fill_cannot_be_rewritten(self):
  self.engine.submit_entry('buy',self.order,'2.10');self.qty=2;self.state='ORDER_STATE_CANCELED'
  with self.assertRaises(ValueError):self.engine.reconcile('buy')
  self.assertEqual(self.engine.available(self.order['marketSlug'],self.order['intent']),5)

 def test_documented_no_price_example(self):
  for action in ('BUY','SELL'):
   order=limit_order(self.order['marketSlug'],'NO',action,1,'.83')
   self.assertEqual(order['price']['value'],'0.17')
  self.assertEqual(limit_order(self.order['marketSlug'],'YES','BUY',1,'.83')['price']['value'],'0.83')
 def test_fractional_roundtrip_exit_and_restart(self):
  orders={}
  def transport(method,path,h,body):
   if path=='/v1/order/preview':
    req=json.loads(body)['request'];return {'order':{**req,'commissionNotionalTotalCollected':{'value':'.01','currency':'USD'}}}
   if path=='/v1/orders':
    req=json.loads(body);ident='fraction-'+str(len(orders)+1)
    orders[ident]={**req,'id':ident,'state':'ORDER_STATE_FILLED','cumQuantity':req['quantity'],
     'avgPx':req['price'],'commissionNotionalTotalCollected':{'value':'.01','currency':'USD'}}
    return {'id':ident}
   return {'order':orders[path.rsplit('/',1)[-1]]}
  self.client.transport=transport
  buy=limit_order(self.order['marketSlug'],'NO','BUY','1.237','.4')
  self.engine.submit_entry('fraction-buy',buy,'.60')
  self.engine.close();self.engine=Execution(self.path,self.client)
  sell=limit_order(self.order['marketSlug'],'NO','SELL','1.237','.6')
  self.engine.submit_exit('fraction-sell',sell,'.10')
  self.assertEqual(self.engine.portfolio()['positions'],[])
  self.assertEqual(self.engine.portfolio()['realized_pnl'],'0.2274')
  self.assertEqual(len(orders),2)
 def test_fractional_quantity_precision_cannot_silently_change(self):
  with self.assertRaisesRegex(ValueError,'precision'):
   limit_order(self.order['marketSlug'],'YES','SELL','1.1234567890123456789','.5')
 def test_account_settlement_credited_once_and_removes_inventory(self):
  self.engine.submit_entry('buy',self.order,'2.10')
  activity={'type':'ACTIVITY_TYPE_POSITION_RESOLUTION','positionResolution':{'marketSlug':self.order['marketSlug'],'tradeId':'resolution-1','side':'POSITION_RESOLUTION_SIDE_SHORT','updateTime':'2026-09-10T12:00:00Z','beforePosition':{'netPositionDecimal':'-5','cost':{'value':'1.96','currency':'USD'},'realized':{'value':'0','currency':'USD'}},'afterPosition':{'expired':True,'netPositionDecimal':'0','cost':{'value':'0','currency':'USD'},'realized':{'value':'3.04','currency':'USD'}}}}
  settlement={'slug':self.order['marketSlug'],'settlement':0}
  self.engine.record_settlement(activity,settlement,1800000000)
  self.engine.record_settlement(activity,settlement,1800000001)
  self.assertEqual(self.engine.portfolio()['realized_pnl'],'3.04');self.assertEqual(self.engine.portfolio()['positions'],[])
  self.assertEqual(self.engine.available(self.order['marketSlug'],self.order['intent']),0)
  with self.assertRaises(ValueError):self.engine.record_settlement(activity,{**settlement,'settlement':1},1800000002)
  self.assertEqual(self.engine.portfolio()['realized_pnl'],'3.04')
