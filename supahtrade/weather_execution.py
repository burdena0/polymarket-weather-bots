"""Durable weather entry execution. All amounts use USD millionths.

The caller supplies a reviewed US weather contract and a freshly validated order.
No strategy is armed by constructing this adapter. Unknown submissions are never
retried automatically. This initial entry budget is cumulative, not recycled.
"""
import json
import time
from decimal import Decimal, ROUND_CEILING
from .order_journal import OrderJournal
from .polymarket_us import decimal, identifier

SCALE=1000000
TERMINAL={'ORDER_STATE_FILLED','ORDER_STATE_CANCELED','ORDER_STATE_REJECTED','ORDER_STATE_EXPIRED'}
def money(amount):
    if amount.get('currency')!='USD': raise ValueError('Expected USD')
    return decimal(amount['value'])
def units(value): return int((decimal(value)*SCALE).to_integral_value(rounding=ROUND_CEILING))
def outcome_price(order,field='price'):
    price=money(order[field])
    if price>1:raise ValueError('Price exceeds binary payout')
    return Decimal(1)-price if order['intent'].endswith('_SHORT') else price

class Execution:
    def __init__(self,path,client,budget='10',per_order='5'):
        self.client=client;self.per_order=decimal(per_order)
        self.journal=OrderJournal(path,units(budget))
        self.journal.db.execute('CREATE TABLE IF NOT EXISTS venue_orders(local_id TEXT PRIMARY KEY, venue_id TEXT UNIQUE, snapshot TEXT)')
        self.journal.db.execute('CREATE TABLE IF NOT EXISTS submission_diagnostics(local_id TEXT PRIMARY KEY,at REAL,stage TEXT,error_type TEXT,http_status INTEGER,venue_id TEXT)')
        self.journal.db.execute('CREATE TABLE IF NOT EXISTS settlements(receipt TEXT PRIMARY KEY,slug TEXT,outcome TEXT,payload TEXT,UNIQUE(slug,outcome))')
    def close(self): self.journal.close()
    def portfolio(self):
        """Bot-only fill accounting; no venue mark is presented as realized P&L."""
        positions={};realized=Decimal(0);fees=Decimal(0);spent=Decimal(0);received=Decimal(0)
        rows=self.journal.db.execute('SELECT o.intent,v.snapshot FROM orders o JOIN venue_orders v ON o.id=v.local_id ORDER BY o.rowid')
        for row in rows:
            if not row['snapshot']:continue
            order=json.loads(row['snapshot']);qty=decimal(order['cumQuantity']);fee=money(order['commissionNotionalTotalCollected'])
            fees+=fee;key=(order['marketSlug'],order['intent'].rsplit('_',1)[-1])
            position=positions.setdefault(key,{'quantity':Decimal(0),'cost':Decimal(0)})
            gross=outcome_price(order,'avgPx')*qty if qty else Decimal(0)
            if '_BUY_' in order['intent']:
                spent+=gross+fee
                if qty:position['quantity']+=qty;position['cost']+=gross+fee
                else:realized-=fee
            else:
                if qty>position['quantity']:raise ValueError('Sell exceeds tracked inventory')
                basis=position['cost']*qty/position['quantity'] if qty else Decimal(0)
                position['quantity']-=qty;position['cost']-=basis
                received+=gross-fee;realized+=gross-fee-basis
        settlement_proceeds=Decimal(0)
        for row in self.journal.db.execute('SELECT payload FROM settlements'):
            item=json.loads(row[0]);key=(item['slug'],'SHORT' if item['outcome']=='NO' else 'LONG')
            position=positions.get(key)
            if not position or position['quantity']!=decimal(item['quantity']) or position['cost']!=decimal(item['cost']):
                raise ValueError('Settlement inventory no longer matches ledger')
            position['quantity']=Decimal(0);position['cost']=Decimal(0)
            realized+=Decimal(item['realized_pnl']);settlement_proceeds+=decimal(item['payout'])
        return dict(realized_pnl=str(realized),fees=str(fees),purchase_debits=str(spent),sale_proceeds=str(received),settlement_proceeds=str(settlement_proceeds),
                    reserved_debit_units=self.journal.reserved(),unresolved_orders=len(self.journal.unresolved()),
                    positions=[dict(slug=k[0],outcome='NO' if k[1]=='SHORT' else 'YES',quantity=str(v['quantity']),cost=str(v['cost'])) for k,v in positions.items() if v['quantity']],
                    coverage='Bot fills and corroborated account position resolutions; excludes external trades and unrealized marks. Settlement is not a cash-transfer receipt.')
    def record_settlement(self,activity,settlement,received):
        from .weather_us_settlement import reconcile_resolution
        receipt=identifier(activity['positionResolution']['tradeId'])
        with self.journal.atomic():
            existing=self.journal.db.execute('SELECT payload FROM settlements WHERE receipt=?',(receipt,)).fetchone()
            if existing:
                old=json.loads(existing[0]);checked=reconcile_resolution(activity,old,settlement,received)
                if any(checked[k]!=old[k] for k in ('slug','outcome','quantity','cost','payout','realized_pnl')):
                    raise ValueError('Conflicting settlement receipt')
                return old
            candidates=[p for p in self.portfolio()['positions'] if p['slug']==activity['positionResolution']['marketSlug']]
            matches=[]
            for position in candidates:
                try: matches.append(reconcile_resolution(activity,position,settlement,received))
                except ValueError:pass
            if len(matches)!=1:raise ValueError('Settlement not uniquely attributable to bot')
            item=matches[0]
            self.journal.db.execute('INSERT INTO settlements VALUES(?,?,?,?)',(receipt,item['slug'],item['outcome'],json.dumps(item)))
            return item
    def submit_entry(self,local_id,order,max_debit):
        return self._submit(local_id,order,max_debit,False)
    def submit_exit(self,local_id,order,max_fee):
        return self._submit(local_id,order,max_fee,True)
    def available(self,slug,intent):
        """Own confirmed buys minus all reserved sells; called under write lock."""
        side=intent.rsplit('_',1)[-1];quantity=Decimal(0)
        rows=self.journal.db.execute('SELECT o.intent,o.state,v.snapshot FROM orders o LEFT JOIN venue_orders v ON o.id=v.local_id')
        for row in rows:
            request=json.loads(row['intent'])
            if request['marketSlug']!=slug or not request['intent'].endswith('_'+side):continue
            snapshot=json.loads(row['snapshot']) if row['snapshot'] else None
            if '_BUY_' in request['intent']:
                if snapshot: quantity+=decimal(snapshot['cumQuantity'])
            elif row['state'] not in ('settled','rejected'):
                quantity-=decimal(request['quantity'])
            elif snapshot:
                quantity-=decimal(snapshot['cumQuantity'])
        for row in self.journal.db.execute('SELECT payload FROM settlements WHERE slug=? AND outcome=?',(slug,'NO' if side=='SHORT' else 'YES')):
            quantity-=decimal(json.loads(row[0])['quantity'])
        return quantity
    def _submit(self,local_id,order,max_debit,is_exit):
        cap=decimal(max_debit)
        if not self.client.allow_orders: raise ValueError('Execution not armed')
        if not order.get('marketSlug','').startswith('tc-temp-'): raise ValueError('Weather only')
        action='SELL' if is_exit else 'BUY'
        if order.get('intent') not in ('ORDER_INTENT_'+action+'_LONG','ORDER_INTENT_'+action+'_SHORT'): raise ValueError('Wrong order intent')
        if order.get('type')!='ORDER_TYPE_LIMIT' or order.get('tif')!='TIME_IN_FORCE_IMMEDIATE_OR_CANCEL': raise ValueError('IOC limit required')
        principal=outcome_price(order)*decimal(order['quantity'])
        if not 0<cap<=self.per_order or principal<=0 or (not is_exit and principal>=cap): raise ValueError('Invalid fee-inclusive cap')
        def inventory_guard():
            if is_exit and self.available(order['marketSlug'],order['intent'])<decimal(order['quantity']):
                raise ValueError('Insufficient unreserved bot holdings')
            if not is_exit:
                opposite='ORDER_INTENT_BUY_'+('LONG' if order['intent'].endswith('_SHORT') else 'SHORT')
                if self.available(order['marketSlug'],opposite)>0:
                    raise ValueError('Close opposite outcome before opening this side')
                budget=self.journal.db.execute('SELECT budget FROM limits WHERE id=1').fetchone()[0]
                if self.journal.reserved()+units(cap)>budget-units('.50'):
                    raise ValueError('Preserve exit fee reserve')
        state=self.journal.prepare(local_id,order,units(cap),guard=inventory_guard)
        if state['state']!='prepared': return self.journal.get(local_id)
        if self.journal.unresolved(): raise ValueError('Reconcile outstanding orders first')
        preview=self.client.preview(order)['order']
        if preview.get('marketSlug')!=order['marketSlug'] or preview.get('intent')!=order['intent']:
            raise ValueError('Preview identity mismatch')
        if preview.get('state')=='ORDER_STATE_REJECTED':
            return self.journal.reject_prepared(local_id)
        if decimal(preview['quantity'])!=decimal(order['quantity']) or money(preview['price'])!=money(order['price']):
            raise ValueError('Preview changed quantity or limit')
        fee=money(preview['commissionNotionalTotalCollected'])
        if (fee if is_exit else principal+fee)>cap: raise ValueError('Preview exceeds cap')
        if not self.journal.claim_dispatch(local_id): return self.journal.get(local_id)
        stage='create_request';venue_id=None
        try:
            result=self.client.create(order)
            stage='parse_create_response';venue_id=identifier(result['id'])
            stage='save_venue_id'
            with self.journal.atomic():
                self.journal.db.execute('INSERT INTO venue_orders(local_id,venue_id) VALUES(?,?)',(local_id,venue_id))
        except Exception as exc:
            # Retain diagnostic evidence without secrets or arbitrary response text.
            # A status code alone does not prove that an order was never accepted.
            with self.journal.atomic():
                self.journal.db.execute('INSERT OR REPLACE INTO submission_diagnostics VALUES(?,?,?,?,?,?)',
                    (local_id,time.time(),stage,type(exc).__name__,getattr(exc,'http_status',None),venue_id))
            self.journal.mark_unknown(local_id)
            return self.journal.get(local_id)
        return self.reconcile(local_id)
    def reconcile(self,local_id):
        record=self.journal.get(local_id)
        mapping=self.journal.db.execute('SELECT venue_id FROM venue_orders WHERE local_id=?',(local_id,)).fetchone()
        if not mapping: return record  # Unknown ID requires manual venue reconciliation; no resend.
        order=self.client.order(mapping[0])['order'];intent=json.loads(record['intent'])
        if order.get('id')!=mapping[0] or any(order.get(k)!=intent[k] for k in ('marketSlug','intent')):
            raise ValueError('Venue order identity mismatch')
        quantity=decimal(order['cumQuantity'])
        if quantity>decimal(intent['quantity']): raise ValueError('Overfill reported')
        if order.get('state')=='ORDER_STATE_FILLED' and quantity!=decimal(intent['quantity']):
            raise ValueError('Filled order quantity mismatch')
        previous=self.journal.db.execute('SELECT snapshot FROM venue_orders WHERE local_id=?',(local_id,)).fetchone()[0]
        if previous:
            previous=json.loads(previous)
            if quantity<decimal(previous['cumQuantity']):raise ValueError('Cumulative fills decreased')
            if record['state'] in ('settled','rejected'):
                if any(order.get(k)!=previous.get(k) for k in ('state','cumQuantity','avgPx','commissionNotionalTotalCollected')):
                    raise ValueError('Conflicting terminal order')
                return record
        fee=money(order['commissionNotionalTotalCollected'])
        if quantity:
            average=outcome_price(order,'avgPx');limit=outcome_price(intent)
            if average>1 or ('_BUY_' in intent['intent'] and average>limit) or ('_SELL_' in intent['intent'] and average<limit):
                raise ValueError('Fill violates limit')
        with self.journal.atomic():
            self.journal.db.execute('UPDATE venue_orders SET snapshot=? WHERE local_id=?',(json.dumps(order),local_id))
        if order.get('state') not in TERMINAL: return self.journal.get(local_id)
        debit=(outcome_price(order,'avgPx')*quantity if quantity and '_BUY_' in intent['intent'] else Decimal(0))+fee
        self.journal.reconcile(local_id,receipt=mapping[0],actual_debit_units=units(debit),rejected=not debit and not quantity)
        return self.journal.get(local_id)
    def cancel(self,local_id):
        record=self.journal.get(local_id)
        if record['state'] in ('settled','rejected'): return record
        mapping=self.journal.db.execute('SELECT venue_id FROM venue_orders WHERE local_id=?',(local_id,)).fetchone()
        if not mapping: raise ValueError('Unknown venue ID; cannot cancel blindly')
        self.client.cancel(mapping[0],json.loads(record['intent'])['marketSlug'])
        # A cancel acknowledgment is not evidence that no fills occurred.
        return self.reconcile(local_id)

    def recover_order_id(self,local_id,venue_id):
        """Operator-supplied association only; never searches or sends an order.

        Matching economics cannot prove historical identity by themselves. The
        operator must identify the order from the venue's submission records.
        """
        venue_id=identifier(venue_id)
        record=self.journal.get(local_id)
        if record['state'] not in ('inflight','unknown'):
            raise ValueError('Recovery requires an unresolved dispatched order')
        intent=json.loads(record['intent'])
        order=self.client.order(venue_id)['order']
        if order.get('id')!=venue_id or any(order.get(k)!=intent[k] for k in ('marketSlug','intent','type','tif')):
            raise ValueError('Recovery order identity mismatch')
        if decimal(order['quantity'])!=decimal(intent['quantity']) or money(order['price'])!=money(intent['price']):
            raise ValueError('Recovery quantity or limit mismatch')
        with self.journal.atomic():
            current=self.journal.get(local_id)
            if current['state'] not in ('inflight','unknown'):
                raise ValueError('Order changed during recovery')
            mapping=self.journal.db.execute('SELECT venue_id FROM venue_orders WHERE local_id=?',(local_id,)).fetchone()
            if mapping and mapping[0]!=venue_id:raise ValueError('Existing order association differs')
            owner=self.journal.db.execute('SELECT local_id FROM venue_orders WHERE venue_id=?',(venue_id,)).fetchone()
            if owner and owner[0]!=local_id:raise ValueError('Broker order already associated')
            self.journal.db.execute('INSERT OR IGNORE INTO venue_orders(local_id,venue_id) VALUES(?,?)',(local_id,venue_id))
        return self.reconcile(local_id)
