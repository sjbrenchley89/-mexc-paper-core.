"""Only official, public MEXC Spot V3 GET endpoints. No authenticated endpoints."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = 'https://api.mexc.com'


def get(path, **params):
    url = BASE + path + '?' + urllib.parse.urlencode(params)
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=8) as response:
                return json.load(response)
        except urllib.error.HTTPError as exc:
            if exc.code not in (429, 500, 502, 503, 504) or attempt == 2:
                raise RuntimeError(f'MEXC public request failed (HTTP {exc.code}); no trade performed.') from None
            time.sleep(min(4, 2**attempt))
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise RuntimeError('MEXC public data unavailable; entries blocked.') from None
            time.sleep(2**attempt)


def candles(symbol, interval='1h', limit=200):
    rows = get('/api/v3/klines', symbol=symbol, interval=interval, limit=limit)
    now_ms = int(time.time()*1000)
    return [dict(time=float(r[0])/1000, open=float(r[1]), high=float(r[2]),
                 low=float(r[3]), close=float(r[4]), volume=float(r[5]))
            for r in rows if float(r[6]) < now_ms]


def price(symbol):
    return float(get('/api/v3/ticker/price', symbol=symbol)['price'])


def liquidity(symbol):
    book = get('/api/v3/depth', symbol=symbol, limit=100)
    bid, ask = float(book['bids'][0][0]), float(book['asks'][0][0])
    mid = (bid+ask)/2
    bids = sum(float(p)*float(q) for p, q in book['bids'] if float(p) >= mid*0.995)
    asks = sum(float(p)*float(q) for p, q in book['asks'] if float(p) <= mid*1.005)
    info = get('/api/v3/exchangeInfo', symbol=symbol)['symbols'][0]
    if info['status'] not in ('ENABLED', 'TRADING', '1', 1):
        raise ValueError('Symbol is not enabled for spot trading.')
    filters = {f['filterType']: f for f in info['filters']}
    lot = filters.get('LOT_SIZE', {})
    minimum = filters.get('MIN_NOTIONAL', filters.get('NOTIONAL', {}))
    # Refuse simulation entries when required exchange rules are absent; never guess them.
    if 'stepSize' not in lot or 'minQty' not in lot or 'minNotional' not in minimum:
        raise ValueError('Required symbol filters absent; confirm current official exchange schema.')
    return dict(spread=(ask-bid)/mid, depth=min(bids, asks), step=float(lot['stepSize']),
                min_qty=float(lot['minQty']), min_notional=float(minimum['minNotional']))


def scan(symbols):
    tickers = {r['symbol']: r for r in get('/api/v3/ticker/24hr')}
    candidates = []
    for symbol in symbols:
        row = tickers.get(symbol)
        if not row or float(row['quoteVolume']) < 1_000_000 or not 1 <= float(row['priceChangePercent']) <= 20:
            continue
        bars = candles(symbol)
        from strategies import evaluate
        action, i = evaluate(bars, 'breakout')
        liquid = liquidity(symbol)
        if liquid['spread'] > 0.002 or liquid['depth'] < 1000 or not 0.003 <= i['atr']/bars[-1]['close'] <= 0.03:
            continue
        spike = bars[-1]['volume']/(sum(b['volume'] for b in bars[-21:-1])/20)
        change = float(row['priceChangePercent'])
        score = spike*change/(1+1000*liquid['spread'])
        candidates.append(dict(symbol=symbol, action=action, score=score, volume_spike=spike,
                               change_pct=change, atr=i['atr'], candle_time=bars[-1]['time'], liquidity=liquid))
    return sorted(candidates, key=lambda r:r['score'], reverse=True)
