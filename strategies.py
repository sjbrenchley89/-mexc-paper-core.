"""Exactly two built-in closed-candle strategies; no profitability claims."""
import math


def ema(values, period):
    out, value = [], values[0]
    for x in values:
        value += 2/(period+1)*(x-value)
        out.append(value)
    return out


def indicators(bars):
    if len(bars) < 60:
        raise ValueError('At least 60 closed candles required.')
    close = [b['close'] for b in bars]
    gains = [max(0, close[i]-close[i-1]) for i in range(1, len(close))]
    losses = [max(0, close[i-1]-close[i]) for i in range(1, len(close))]
    # Wilder smoothing starts with the first 14 changes.
    gain, loss = sum(gains[:14])/14, sum(losses[:14])/14
    for g, l in zip(gains[14:], losses[14:]):
        gain, loss = (13*gain+g)/14, (13*loss+l)/14
    rsi = 50 if gain == loss == 0 else 100 if loss == 0 else 100-100/(1+gain/loss)
    tr = [max(bars[i]['high']-bars[i]['low'], abs(bars[i]['high']-close[i-1]),
              abs(bars[i]['low']-close[i-1])) for i in range(1, len(bars))]
    atr = sum(tr[:14])/14
    for t in tr[14:]:
        atr = (13*atr+t)/14
    e9, e21 = ema(close, 9), ema(close, 21)
    macd = [a-b for a, b in zip(ema(close, 12), ema(close, 26))]
    signal = ema(macd, 9)
    result = dict(atr=atr, rsi=rsi, e9=e9[-1], e21=e21[-1],
                  previous_e9=e9[-2], previous_e21=e21[-2], hist=macd[-1]-signal[-1])
    if not all(math.isfinite(v) for v in result.values()):
        raise ValueError('Invalid indicators.')
    return result


def evaluate(bars, strategy):
    i = indicators(bars)
    if strategy == 'technical':
        buy = (i['previous_e9'] <= i['previous_e21'] and i['e9'] > i['e21']
               and 50 <= i['rsi'] <= 65 and i['hist'] > 0)
        sell = (i['previous_e9'] >= i['previous_e21'] and i['e9'] < i['e21']) or i['rsi'] < 45
    elif strategy == 'breakout':
        previous = bars[-21:-1]
        average_volume = sum(b['volume'] for b in previous)/20
        buy = (bars[-1]['close'] > max(b['high'] for b in previous)
               and average_volume > 0 and bars[-1]['volume'] >= 2*average_volume)
        sell = bars[-1]['close'] < min(b['low'] for b in bars[-11:-1])
    else:
        raise ValueError('Unknown strategy. News and portal rules are not implemented.')
    return ('sell' if sell else 'buy' if buy else 'hold'), i
