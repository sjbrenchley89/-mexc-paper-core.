"""Persistent, transactional paper ledger. No exchange order endpoint exists here."""
import copy
import json
import math
import sqlite3
from datetime import datetime, timezone

TOTAL_CAP = 50.0
POSITION_CAP = 10.0
FLOOR = 25.0
DAILY_LIMIT = 0.10


def utc_day(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()


def finite(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


class Ledger:
    def __init__(self, path, config):
        self.path, self.config = str(path), config

    def initial(self, now):
        cash = float(self.config.get('paper_cash', 50))
        if not finite(cash) or not FLOOR <= cash <= TOTAL_CAP:
            raise ValueError('Paper cash must be between 25 and 50.')
        return dict(mode='paper', cash=cash, positions={}, prices={}, day=utc_day(now),
                    day_start=cash, paused=False, killed=False, review_required=False,
                    breach_day=None, floor_breached=False, seen=[], events=[], reviews=[])

    def transact(self, now, operation):
        with sqlite3.connect(self.path, timeout=10) as db:
            db.execute('CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY, data TEXT NOT NULL)')
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM ledger WHERE id=1').fetchone()
            state = json.loads(row[0]) if row else self.initial(now)
            result = operation(state)
            db.execute('INSERT OR REPLACE INTO ledger VALUES (1,?)', (json.dumps(state, allow_nan=False),))
            return result

    @staticmethod
    def equity(state):
        return state['cash'] + sum(p['qty'] * state['prices'].get(s, p['entry_price'])
                                   for s, p in state['positions'].items())

    def limits(self, state, now):
        equity = self.equity(state)
        # Check against yesterday's baseline BEFORE changing the UTC day.
        daily = min(DAILY_LIMIT, float(self.config.get('daily_loss', DAILY_LIMIT)))
        if equity <= state['day_start'] * (1 - daily):
            state.update(paused=True, review_required=True)
            state['breach_day'] = state['day']
        if equity < max(FLOOR, float(self.config.get('floor', FLOOR))):
            state.update(paused=True, review_required=True, floor_breached=True)
        if state['day'] != utc_day(now):
            state.update(day=utc_day(now), day_start=equity)
        return equity

    def event(self, state, now, **data):
        state['events'].append(dict(timestamp=now, **data))

    def status(self, now):
        def operation(state):
            equity = self.limits(state, now)
            daily = min(DAILY_LIMIT, float(self.config.get('daily_loss', DAILY_LIMIT)))
            result = copy.deepcopy(state)
            result.update(equity=equity, daily_pnl=equity-state['day_start'],
                          daily_limit_amount=state['day_start']*daily,
                          distance_to_floor=equity-max(FLOOR, float(self.config.get('floor', FLOOR))),
                          note='PAPER ONLY; values are USDT, assumed USD-equivalent; marks may be stale.')
            return result
        return self.transact(now, operation)

    def control(self, command, now, note=''):
        def operation(state):
            equity = self.limits(state, now)
            if command == 'pause':
                state['paused'] = True
            elif command == 'kill':
                state.update(killed=True, paused=True, review_required=True)
            elif command == 'resume':
                if state['review_required'] or state['killed']:
                    raise ValueError('Manual review/re-arm required; resume cannot clear a safety halt.')
                state['paused'] = False
            elif command == 'review-rearm':
                if not note.strip():
                    raise ValueError('A written review note is required.')
                if state['breach_day'] == utc_day(now):
                    raise ValueError('Daily loss breach: wait until the next UTC day AND review.')
                if equity < max(FLOOR, float(self.config.get('floor', FLOOR))):
                    raise ValueError('Equity remains below the floor; manual intervention required.')
                state['reviews'].append(dict(timestamp=now, note=note))
                state.update(paused=False, killed=False, review_required=False, floor_breached=False)
            else:
                raise ValueError('Unknown control.')
            self.event(state, now, kind='control', command=command)
            return dict(command=command, paused=state['paused'], killed=state['killed'])
        return self.transact(now, operation)

    def _sell(self, state, symbol, price, now, reason):
        p = state['positions'].pop(symbol)
        sell_price = price * (1-float(self.config['slippage']))
        gross = p['qty']*sell_price
        fee = gross*float(self.config['fee'])
        proceeds = gross-fee
        state['cash'] += proceeds
        self.event(state, now, kind='fill', action='sell', symbol=symbol,
                   strategy=p['strategy'], source=p['source'], qty=p['qty'], price=sell_price,
                   fee=fee, pnl=proceeds-p['cost'], reason=reason)

    def mark(self, quotes, now):
        def operation(state):
            for symbol, price in quotes.items():
                if not finite(price) or price <= 0:
                    raise ValueError('Invalid market price.')
                state['prices'][symbol] = price
            self.limits(state, now)
            # Risk-reducing exits are allowed even during pause, kill, or a loss breach.
            for symbol, p in list(state['positions'].items()):
                if symbol not in quotes:
                    continue
                price = quotes[symbol]
                cap = min(POSITION_CAP, float(self.config['position_cap']))
                if state['review_required'] or price <= p['stop'] or price >= p['take_profit'] or p['qty']*price > cap:
                    reason = ('safety_halt' if state['review_required'] else 'stop' if price <= p['stop']
                              else 'take_profit' if price >= p['take_profit'] else 'exposure_cap')
                    self._sell(state, symbol, price, now, reason)
            self.limits(state, now)
            return self.equity(state)
        return self.transact(now, operation)

    def signal(self, signal_id, symbol, action, price, atr, source, strategy, now,
               source_time=None, liquidity=None):
        def operation(state):
            if signal_id in state['seen']:
                return dict(accepted=False, reason='duplicate')
            state['seen'].append(signal_id)
            def reject(reason):
                self.event(state, now, kind='rejection', id=signal_id, source=source, reason=reason)
                return dict(accepted=False, reason=reason)
            if symbol not in self.config['symbols']:
                return reject('symbol_not_allowed')
            if action not in ('buy', 'sell') or source not in ('tradingview', 'scanner', 'demo', 'backtest'):
                return reject('unsupported_signal_source_or_action')
            if source == 'scanner' and self.config['option'] != 'B':
                return reject('scanner_disabled')
            if not finite(price) or price <= 0 or not finite(atr) or atr <= 0:
                return reject('invalid_price_or_atr')
            state['prices'][symbol] = price
            equity = self.limits(state, now)
            if action == 'sell':
                if symbol not in state['positions']:
                    return reject('no_bot_position')
                self._sell(state, symbol, price, now, 'signal')
                self.limits(state, now)
                return dict(accepted=True, mode='paper', action='sell')
            if state['paused'] or state['killed'] or state['review_required']:
                return reject('safety_halt')
            if symbol in state['positions']:
                return reject('position_already_open')
            if len(state['positions']) >= min(5, int(self.config['max_positions'])):
                return reject('max_positions')
            if source_time is not None and (not finite(source_time) or not 0 <= now-source_time <= 120):
                return reject('stale_or_future_signal')
            if source not in ('demo', 'backtest'):
                if not liquidity or liquidity['spread'] > 0.002 or liquidity['depth'] < 1000:
                    return reject('liquidity_filter')
            fee, slip = float(self.config['fee']), float(self.config['slippage'])
            fill_price = price*(1+slip)
            stop = price-2*atr
            if stop <= 0 or not 0.003 <= atr/price <= 0.03:
                return reject('volatility_filter')
            # Include estimated fees on both sides and adverse entry/exit slippage.
            unit_loss = fill_price*(1+fee)-stop*(1-slip)*(1-fee)
            risk_cash = equity*min(0.01, float(self.config['risk_per_trade']))
            allocated = sum(p['cost'] for p in state['positions'].values())
            total_cap = min(TOTAL_CAP, float(self.config['total_cap']))
            position_cap = min(POSITION_CAP, float(self.config['position_cap']))
            # Leave 5% headroom for appreciation before the marked exposure cap.
            qty = min(risk_cash/unit_loss, .95*position_cap/(fill_price*(1+fee)),
                      max(0, total_cap-allocated)/(fill_price*(1+fee)),
                      state['cash']/(fill_price*(1+fee)))
            # Exchange symbol filters are applied by the market adapter before submission.
            if liquidity:
                step = liquidity['step']
                if step > 0:
                    qty = math.floor(qty/step)*step
                if qty < liquidity['min_qty'] or qty*price < liquidity['min_notional']:
                    return reject('exchange_minimum_or_step')
            cost = qty*fill_price*(1+fee)
            if cost <= 0 or cost < float(self.config['min_paper_order']):
                return reject('order_too_small')
            state['cash'] -= cost
            state['positions'][symbol] = dict(qty=qty, entry_price=fill_price, cost=cost,
                stop=stop, take_profit=price+4*atr, source=source, strategy=strategy)
            self.event(state, now, kind='fill', action='buy', id=signal_id, symbol=symbol,
                price=fill_price, qty=qty, fee=qty*fill_price*fee, source=source, strategy=strategy,
                latency=None if source_time is None else now-source_time)
            self.limits(state, now)
            return dict(accepted=True, mode='paper', action='buy', cost=cost, qty=qty)
        return self.transact(now, operation)
