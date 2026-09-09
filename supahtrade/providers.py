import json,time
from urllib.error import HTTPError,URLError
from urllib.parse import urlparse
from urllib.request import Request,urlopen


class DataError(Exception):
    pass

class Http:

    def __init__(self, timeout=12, attempts=2):
        (self.timeout, self.attempts) = (timeout, attempts)

    def json(self, url, payload=None, headers=None):
        if urlparse(url).scheme != 'https':
            raise DataError('HTTPS required for external providers')
        hdr = {'Accept': 'application/json', 'User-Agent': 'SupahTrade/0.1 (read-only research)'}
        hdr.update(headers or {})
        body = None
        if payload is not None:
            if payload.get('method') not in {'getAccountInfo', 'getTokenLargestAccounts', 'getSlot', 'getSignaturesForAddress', 'getTransaction'}:
                raise DataError('RPC method is not on the read-only allowlist')
            body = json.dumps(payload).encode()
            hdr['Content-Type'] = 'application/json'
        for attempt in range(self.attempts):
            try:
                with urlopen(Request(url, data=body, headers=hdr), timeout=self.timeout) as response:
                    raw = response.read(8000001)
                if len(raw) > 8000000:
                    raise DataError('Provider response too large')
                return json.loads(raw)
            except HTTPError as exc:
                if exc.code not in {429, 500, 502, 503, 504} or attempt + 1 == self.attempts:
                    raise DataError(f'HTTP {exc.code}') from None
            except (URLError, TimeoutError, OSError):
                if attempt + 1 == self.attempts:
                    raise DataError('Provider connection failed') from None
            except (ValueError, UnicodeError):
                raise DataError('Invalid provider JSON') from None
            time.sleep(min(2 ** attempt, 4))
