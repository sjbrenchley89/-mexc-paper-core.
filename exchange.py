"""Signed MEXC Spot V3 client. Keys come only from the environment and are never printed or logged."""
import hashlib
import hmac
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

BASE = 'https://api.mexc.com'


class ExchangeError(Exception):
    """The exchange answered with a definite rejection."""

    def __init__(self, message, code=None, status=None):
        super().__init__(message)
        self.code, self.status = code, status


class RateLimited(ExchangeError):
    def __init__(self, retry_after):
        super().__init__('MEXC rate limit reached; stop sending requests.', status=429)
        self.retry_after = retry_after


class Uncertain(Exception):
    """The request may or may not have reached the exchange. Query before doing anything else."""


def fmt(value):
    text = format(Decimal(str(value)), 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


class MexcClient:
    def __init__(self, key, secret, base=BASE, recv_window=5000, opener=None, clock=time.time):
        if not key or not secret:
            raise ValueError('MEXC_API_KEY and MEXC_API_SECRET must both be set in the environment.')
        self._key, self._secret = key, secret.encode()
        self.base, self.recv_window, self.clock = base, recv_window, clock
        self.opener = opener or urllib.request.urlopen

    def __repr__(self):
        return 'MexcClient(credentials hidden)'

    @classmethod
    def from_env(cls):
        return cls(os.environ.get('MEXC_API_KEY', ''), os.environ.get('MEXC_API_SECRET', ''))

    def request(self, method, path, params=None, signed=True):
        params = dict(params or {})
        if signed:
            params['recvWindow'] = self.recv_window
            params['timestamp'] = int(self.clock()*1000)
        query = urllib.parse.urlencode(params)
        if signed:
            query += '&signature='+hmac.new(self._secret, query.encode(), hashlib.sha256).hexdigest()
        headers = {'Content-Type': 'application/json'}
        if signed:
            headers['X-MEXC-APIKEY'] = self._key
        request = urllib.request.Request(f'{self.base}{path}?{query}', headers=headers, method=method,
                                         data=b'' if method == 'POST' else None)
        try:
            with self.opener(request, timeout=10) as response:
                body = response.read()
        except urllib.error.HTTPError as exc:
            self._raise_for(exc)
        except (urllib.error.URLError, TimeoutError, OSError):
            raise Uncertain('Network failure; the outcome is unknown.') from None
        try:
            return json.loads(body or b'{}')
        except ValueError:
            raise Uncertain('Unreadable reply from MEXC; the outcome is unknown.') from None

    @staticmethod
    def _raise_for(exc):
        status = exc.code
        if status in (418, 429):
            try:
                wait = float(exc.headers.get('Retry-After', 60))
            except (TypeError, ValueError):
                wait = 60.0
            raise RateLimited(wait) from None
        if status >= 500:
            raise Uncertain(f'MEXC server error (HTTP {status}); the outcome is unknown.') from None
        try:
            detail = json.loads(exc.read() or b'{}')
        except ValueError:
            detail = {}
        code, message = detail.get('code'), str(detail.get('msg', ''))[:200]
        raise ExchangeError(f'MEXC rejected the request (HTTP {status}, code {code}): {message}',
                            code=code, status=status) from None

    def server_time(self):
        return self.request('GET', '/api/v3/time', signed=False)['serverTime']

    def account(self):
        return self.request('GET', '/api/v3/account')

    def api_key_info(self):
        return self.request('GET', '/api/v3/apiKeyInfo', {'accessKey': self._key})

    @staticmethod
    def _order_params(symbol, side, order_type, client_order_id, quantity, quote_order_qty):
        if (quantity is None) == (quote_order_qty is None):
            raise ValueError('Send exactly one of quantity or quote_order_qty.')
        params = {'symbol': symbol, 'side': side, 'type': order_type}
        if quantity is not None:
            params['quantity'] = fmt(quantity)
        else:
            params['quoteOrderQty'] = fmt(quote_order_qty)
        params['newClientOrderId'] = client_order_id
        return params

    def new_order(self, symbol, side, order_type, client_order_id, quantity=None, quote_order_qty=None):
        return self.request('POST', '/api/v3/order', self._order_params(
            symbol, side, order_type, client_order_id, quantity, quote_order_qty))

    def test_order(self, symbol, side, order_type, client_order_id, quantity=None, quote_order_qty=None):
        return self.request('POST', '/api/v3/order/test', self._order_params(
            symbol, side, order_type, client_order_id, quantity, quote_order_qty))

    def query_order(self, symbol, client_order_id):
        return self.request('GET', '/api/v3/order', {'symbol': symbol, 'origClientOrderId': client_order_id})

    def cancel_order(self, symbol, client_order_id):
        return self.request('DELETE', '/api/v3/order', {'symbol': symbol, 'origClientOrderId': client_order_id})

    def my_trades(self, symbol, order_id):
        return self.request('GET', '/api/v3/myTrades', {'symbol': symbol, 'orderId': order_id})
