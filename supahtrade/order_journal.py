"""Durable order lifecycle for execution-adapter tests. No venue calls or signing."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import sqlite3


def units(value, positive=False):
    if type(value) is not int or value < (1 if positive else 0) or value > 2**63-1:
        raise ValueError('Use nonnegative integer base units within SQLite range')
    return value


class OrderJournal:
    def __init__(self, path, budget_units):
        units(budget_units, True)
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db=sqlite3.connect(str(path),timeout=5,isolation_level=None)
        self.db.row_factory=sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS limits(id INTEGER PRIMARY KEY CHECK(id=1), budget INTEGER NOT NULL, halted INTEGER NOT NULL);
          CREATE TABLE IF NOT EXISTS orders(id TEXT PRIMARY KEY, intent TEXT NOT NULL, fingerprint TEXT NOT NULL,
            cap INTEGER NOT NULL, state TEXT NOT NULL, actual_debit INTEGER, receipt TEXT);
          CREATE TABLE IF NOT EXISTS transitions(sequence INTEGER PRIMARY KEY, order_id TEXT NOT NULL, state TEXT NOT NULL);
          CREATE UNIQUE INDEX IF NOT EXISTS unique_terminal_receipt ON orders(receipt) WHERE receipt IS NOT NULL;
        ''')
        try:
            with self.atomic():
                row=self.db.execute('SELECT budget FROM limits WHERE id=1').fetchone()
                if row and row['budget']!=budget_units:
                    raise ValueError('Budget differs; use a separate journal')
                self.db.execute('INSERT OR IGNORE INTO limits VALUES(1,?,0)',(budget_units,))
        except BaseException:
            self.db.close()
            raise

    @contextmanager
    def atomic(self):
        self.db.execute('BEGIN IMMEDIATE')
        try:
            yield
            self.db.execute('COMMIT')
        except BaseException:
            self.db.execute('ROLLBACK')
            raise

    def close(self):
        self.db.close()

    def get(self, order_id):
        row=self.db.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone()
        if row is None:
            raise ValueError('Unknown order')
        return dict(row)

    def reserved(self):
        # Settled debit stays spent. No fictional recycling of capital after a fill.
        return sum(r['actual_debit'] if r['state']=='settled' else r['cap']
                   for r in self.db.execute("SELECT * FROM orders WHERE state!='rejected'"))

    def prepare(self, order_id, intent, max_debit_units, guard=None):
        units(max_debit_units,True)
        if not isinstance(order_id,str) or not 1<=len(order_id)<=128 or not isinstance(intent,dict) or not intent:
            raise ValueError('Nonempty order ID and intent required')
        payload=json.dumps(intent,sort_keys=True,separators=(',',':'),allow_nan=False)
        digest=hashlib.sha256(payload.encode()).hexdigest()
        with self.atomic():
            existing=self.db.execute('SELECT * FROM orders WHERE id=?',(order_id,)).fetchone()
            if existing:
                if existing['fingerprint']!=digest or existing['cap']!=max_debit_units:
                    raise ValueError('Order ID reused for a different intent')
                return dict(existing)
            if guard is not None:
                guard()
            limits=self.db.execute('SELECT * FROM limits WHERE id=1').fetchone()
            if limits['halted'] or self.reserved()+max_debit_units>limits['budget']:
                raise ValueError('Halted or insufficient unreserved budget')
            self.db.execute("INSERT INTO orders VALUES(?,?,?,?,'prepared',NULL,NULL)",(order_id,payload,digest,max_debit_units))
            self.db.execute("INSERT INTO transitions(order_id,state) VALUES(?,'prepared')",(order_id,))
        return self.get(order_id)

    def claim_dispatch(self, order_id):
        """Commit BEFORE an external send; only one caller receives permission."""
        with self.atomic():
            if self.db.execute('SELECT halted FROM limits WHERE id=1').fetchone()[0]:
                return False
            changed=self.db.execute("UPDATE orders SET state='inflight' WHERE id=? AND state='prepared'",(order_id,)).rowcount
            if changed:
                self.db.execute("INSERT INTO transitions(order_id,state) VALUES(?,'inflight')",(order_id,))
        return bool(changed)

    def reject_prepared(self, order_id):
        """Release a reservation only when dispatch has never been claimed."""
        with self.atomic():
            row=self.get(order_id)
            if row['state']!='prepared':
                raise ValueError('Only an undispatched order can be rejected locally')
            self.db.execute("UPDATE orders SET state='rejected',actual_debit=0 WHERE id=?",(order_id,))
            self.db.execute("INSERT INTO transitions(order_id,state) VALUES(?,'rejected')",(order_id,))
        return self.get(order_id)

    def mark_unknown(self,order_id):
        with self.atomic():
            row=self.get(order_id)
            if row['state'] not in {'inflight','unknown'}:
                raise ValueError('Only dispatched orders may become unknown')
            self.db.execute("UPDATE orders SET state='unknown' WHERE id=?",(order_id,))
            self.db.execute("INSERT INTO transitions(order_id,state) VALUES(?,'unknown')",(order_id,))

    def reconcile(self,order_id,*,receipt,actual_debit_units,rejected=False):
        """Caller must verify a terminal venue receipt, including fees. No lookup here."""
        units(actual_debit_units)
        if not isinstance(receipt,str) or not receipt.strip() or receipt != receipt.strip():
            raise ValueError('Terminal receipt reference required')
        if type(rejected) is not bool:
            raise ValueError('Explicit boolean rejection status required')
        if rejected and actual_debit_units:
            raise ValueError('A failed transaction with a fee is settled spending, not a zero-cost rejection')
        target='rejected' if rejected else 'settled'
        with self.atomic():
            row=self.get(order_id)
            if row['state'] in {'settled','rejected'}:
                if (row['state'],row['receipt'],row['actual_debit'])!=(target,receipt,actual_debit_units):
                    raise ValueError('Conflicting terminal receipt')
                return
            if row['state'] not in {'inflight','unknown'}:
                raise ValueError('Order was not dispatched')
            owner=self.db.execute('SELECT id FROM orders WHERE receipt=?',(receipt,)).fetchone()
            if owner is not None and owner['id']!=order_id:
                raise ValueError('Receipt already reconciled to a different order')
            self.db.execute('UPDATE orders SET state=?,actual_debit=?,receipt=? WHERE id=?',(target,actual_debit_units,receipt,order_id))
            self.db.execute('INSERT INTO transitions(order_id,state) VALUES(?,?)',(order_id,target))
            if actual_debit_units>row['cap']:
                self.db.execute('UPDATE limits SET halted=1 WHERE id=1')

    def unresolved(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM orders WHERE state IN ('inflight','unknown') ORDER BY id")]
