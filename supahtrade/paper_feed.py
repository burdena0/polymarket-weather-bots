"""Bounded public weather-market evidence adapter for local paper simulation."""
import json
import math
from pathlib import Path
import re
import time
from urllib.parse import urlencode

from .providers import Http
from .reference_activity_capture import WALLET
from .weather_fees import verify_weather_schedule
from .weather_settlement import result_for


def instrument(token, condition):
    if (not isinstance(token, str) or not re.fullmatch(r'[0-9]{1,78}', token)
            or not 0 < int(token) < 2**256 or not isinstance(condition, str)
            or not re.fullmatch(r'0x[0-9a-fA-F]{64}', condition)):
        raise ValueError('Invalid public instrument identifier')
    return token, condition.lower()


class PublicFeed:
    def __init__(self, artifact_dir, http=None, clock=None):
        self.artifact_dir = Path(artifact_dir)
        self.artifact_dir.mkdir(parents=True, exist_ok=True)
        self.http = http or Http(timeout=5, attempts=1)
        self.clock = clock or time.time
        self.cache = {}

    def _fetch(self, url):
        started = self.clock()
        try:
            raw = self.http.json(url)
        except Exception as exc:
            self._record(dict(url=url, request_started=started, received=self.clock(),
                              error=type(exc).__name__))
            raise
        received = self.clock()
        self._record(dict(url=url, request_started=started, received=received, data=raw))
        return raw, started, received

    def _record(self, evidence):
        # All URLs are constructed here from validated public identifiers.
        with (self.artifact_dir / 'public-feed.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(evidence, allow_nan=False) + '\n')

    def activity(self):
        end = int(self.clock())
        url = 'https://data-api.polymarket.com/activity?' + urlencode(dict(
            user=WALLET, type='TRADE', limit=100, offset=0, end=end,
            sortBy='TIMESTAMP', sortDirection='DESC'))
        rows, started, received = self._fetch(url)
        if not isinstance(rows, list) or len(rows) > 100:
            raise ValueError('Invalid or oversized activity page')
        selected = []
        for row in rows:
            if (not isinstance(row, dict) or row.get('proxyWallet', '').lower() != WALLET
                    or isinstance(row.get('timestamp'), bool)
                    or not isinstance(row.get('timestamp'), (int, float))
                    or not 0 <= row['timestamp'] <= end):
                raise ValueError('Activity wallet or timestamp mismatch')
            if row.get('type') != 'TRADE' or row.get('side') not in ('BUY', 'SELL'):
                continue
            if 'highest temperature' not in row.get('title', '').lower():
                continue
            instrument(row.get('asset'), row.get('conditionId'))
            selected.append(row)
        return dict(received=received, request_started=started, rows=selected,
                    complete_history=False, page_full=len(rows) == 100)

    def book(self, token, condition):
        token, condition = instrument(token, condition)
        book, started, received = self._fetch('https://clob.polymarket.com/book?token_id=' + token)
        if (not isinstance(book, dict) or book.get('asset_id') != token
                or book.get('market', '').lower() != condition):
            raise ValueError('Book instrument mismatch')
        return dict(received=received, request_started=started, book=book)

    def market(self, token, condition):
        token, condition = instrument(token, condition)
        key = (token, condition)
        now = self.clock()
        cached = self.cache.get(key)
        if cached and (cached['resolved'] or 0 <= now - cached['received'] < 60):
            return dict(cached)
        gamma = None
        for closed in (None, 'true'):
            query = dict(condition_ids=condition, limit=100)
            if closed is not None:
                query['closed'] = closed
            rows, _, _ = self._fetch('https://gamma-api.polymarket.com/markets?' + urlencode(query))
            if not isinstance(rows, list) or len(rows) > 100:
                raise ValueError('Invalid market response')
            matches = [r for r in rows if r.get('conditionId', '').lower() == condition]
            if len(matches) > 1:
                raise ValueError('Ambiguous market')
            if matches:
                gamma = matches[0]
                break
        if gamma is None:
            raise ValueError('Market not found')
        question = gamma.get('question', '')
        if 'highest temperature' not in question.lower():
            raise ValueError('Only highest-temperature markets supported')
        tokens, outcomes = json.loads(gamma['clobTokenIds']), json.loads(gamma['outcomes'])
        if len(tokens) != 2 or len(set(tokens)) != 2 or set(outcomes) != {'Yes', 'No'} or len(outcomes) != 2 or token not in tokens:
            raise ValueError('Invalid binary token mapping')
        outcome = outcomes[tokens.index(token)]
        size = gamma.get('orderMinSize')
        if isinstance(size, bool):
            size = None
        try:
            size = float(size)
            if not math.isfinite(size) or size <= 0:
                size = None
        except (ValueError, TypeError):
            size = None
        info, _, received = self._fetch('https://clob.polymarket.com/clob-markets/' + condition)
        fee_verified = False
        try:
            verify_weather_schedule(gamma, info)
            fee_verified = True
        except (ValueError, KeyError, TypeError):
            pass
        resolved, payout = False, None
        if gamma.get('closed') is True and gamma.get('umaResolutionStatus') == 'resolved':
            clob, _, received = self._fetch('https://clob.polymarket.com/markets/' + condition)
            state = dict(position=dict(condition_id=condition, token=token, outcome=outcome,
                         entered_at=0, shares=1, cost=0), cash=0, realized_pnl=0)
            settlement = result_for(state, gamma, clob, received)
            if settlement['status'] == 'paper_settled':
                resolved, payout = True, int(settlement['modeled_payout'])
        result = dict(resolved=resolved, payout=payout, min_order_size=size,
                      fee_verified=fee_verified, question=question, outcome=outcome,
                      received=received, closed=gamma.get('closed') is True,
                      execution_eligible=False)
        self.cache[key] = dict(result)
        return result
