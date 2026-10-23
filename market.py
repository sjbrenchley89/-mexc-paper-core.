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


# MEXC writes the hourly kline interval as 60m; 1h is not in its documented list.
INTERVALS = {'1h': '60m'}


def candles(symbol, interval='1h', limit=200):
    rows = get('/api/v3/klines', symbol=symbol, interval=INTERVALS.get(interval, interval), limit=limit)
    now_ms = int(time.time()*1000)
    return [dict(time=float(r[0])/1000, open=float(r[1]), high=float(r[2]),
                 low=float(r[3]), close=float(r[4]), volume=float(r[5]))
            for r in rows if float(r[6]) < now_ms]


def price(symbol):
    return float(get('/api/v3/ticker/price', symbol=symbol)['price'])


def symbol_rules(info):
    # Documented MEXC schema: status "1" is online; base/quote rules are plain fields, not filters.
    if str(info.get('status')) not in ('1', 'ENABLED', 'TRADING') or info.get('isSpotTradingAllowed') is not True:
        raise ValueError('Symbol is not enabled for spot trading.')
    filters = {f['filterType']: f for f in info.get('filters', [])}
    lot, notional = filters.get('LOT_SIZE'), filters.get('MIN_NOTIONAL') or filters.get('NOTIONAL')
    try:
        if lot and notional:
            return dict(step=float(lot['stepSize']), min_qty=float(lot['minQty']),
                        min_notional=float(notional['minNotional']))
        minimum = float(info['baseSizePrecision'])
        step = max(minimum, 10**-int(info['baseAssetPrecision']))
        return dict(step=step, min_qty=minimum, min_notional=float(info['quoteAmountPrecision']))
    except (KeyError, TypeError, ValueError):
        raise ValueError('Required symbol rules absent; confirm current official exchange schema.') from None


def symbol_rules_for(symbol):
    return symbol_rules(get('/api/v3/exchangeInfo', symbol=symbol)['symbols'][0])


def trading_status(symbol):
    info = get('/api/v3/exchangeInfo', symbol=symbol)['symbols'][0]
    try:
        symbol_rules(info)
    except ValueError as exc:
        if 'not enabled' in str(exc):
            return dict(ok=False, status=str(info.get('status')))
    return dict(ok=True, status=str(info.get('status')))


def liquidity(symbol):
    book = get('/api/v3/depth', symbol=symbol, limit=100)
    bid, ask = float(book['bids'][0][0]), float(book['asks'][0][0])
    mid = (bid+ask)/2
    bids = sum(float(p)*float(q) for p, q in book['bids'] if float(p) >= mid*0.995)
    asks = sum(float(p)*float(q) for p, q in book['asks'] if float(p) <= mid*1.005)
    rules = symbol_rules(get('/api/v3/exchangeInfo', symbol=symbol)['symbols'][0])
    return dict(spread=(ask-bid)/mid, depth=min(bids, asks), **rules)


def ticker_stats(row):
    # The documented priceChangePercent is a fraction and quoteVolume can be null; derive both.
    last, opened = float(row['lastPrice']), float(row['openPrice'])
    quote = row.get('quoteVolume')
    return dict(change_pct=(last/opened-1)*100 if opened > 0 else None,
                quote_volume=float(quote) if quote not in (None, '') else float(row['volume'])*last)


def scan(symbols):
    tickers = {r['symbol']: r for r in get('/api/v3/ticker/24hr')}
    candidates = []
    for symbol in symbols:
        row = tickers.get(symbol)
        stats = ticker_stats(row) if row else None
        if not stats or stats['change_pct'] is None or stats['quote_volume'] < 1_000_000 \
                or not 1 <= stats['change_pct'] <= 20:
            continue
        bars = candles(symbol)
        from strategies import evaluate
        action, i = evaluate(bars, 'breakout')
        liquid = liquidity(symbol)
        if liquid['spread'] > 0.002 or liquid['depth'] < 1000 or not 0.003 <= i['atr']/bars[-1]['close'] <= 0.03:
            continue
        spike = bars[-1]['volume']/(sum(b['volume'] for b in bars[-21:-1])/20)
        change = stats['change_pct']
        score = spike*change/(1+1000*liquid['spread'])
        candidates.append(dict(symbol=symbol, action=action, score=score, volume_spike=spike,
                               change_pct=change, atr=i['atr'], candle_time=bars[-1]['time'], liquidity=liquid))
    return sorted(candidates, key=lambda r:r['score'], reverse=True)
