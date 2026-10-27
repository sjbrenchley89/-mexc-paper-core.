"""Persistent, transactional ledger. Paper by default; live mode only records intents and exchange fills.

This module never contacts an exchange. In live mode it queues order intents for live.py to submit."""
import copy
import hashlib
import json
import math
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

TOTAL_CAP = 50.0
POSITION_CAP = 10.0
FLOOR = 25.0
DAILY_LIMIT = 0.10


def utc_day(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()


def finite(value):
    return isinstance(value, (float, int)) and not isinstance(value, bool) and math.isfinite(value)


def order_id(prefix, text):
    return prefix+hashlib.sha256(text.encode()).hexdigest()[:30]


class Ledger:
    def __init__(self, path, config, live=False):
        self.path, self.config, self.live = str(path), config, live

    def initial(self, now):
        if self.live:
            return dict(mode='live', cash=0.0, positions={}, prices={}, day=utc_day(now), day_start=0.0,
                        paused=False, killed=False, review_required=False, breach_day=None,
                        floor_breached=False, seen=[], events=[], reviews=[], news_blocks=[],
                        pending={}, synced=False)
        cash = float(self.config.get('paper_cash', 50))
        if not finite(cash) or not FLOOR <= cash <= TOTAL_CAP:
            raise ValueError('Paper cash must be between 25 and 50.')
        return dict(mode='paper', cash=cash, positions={}, prices={}, day=utc_day(now),
                    day_start=cash, paused=False, killed=False, review_required=False,
                    breach_day=None, floor_breached=False, seen=[], events=[], reviews=[],
                    news_blocks=[])

    def transact(self, now, operation):
        with closing(sqlite3.connect(self.path, timeout=10)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS ledger (id INTEGER PRIMARY KEY, data TEXT NOT NULL)')
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT data FROM ledger WHERE id=1').fetchone()
            state = json.loads(row[0]) if row else self.initial(now)
            if state['mode'] != ('live' if self.live else 'paper'):
                raise ValueError(f"This ledger file is a {state['mode']} ledger; use {'--live' if state['mode'] == 'live' else 'the paper ledger'} for it.")
            result = operation(state)
            db.execute('INSERT OR REPLACE INTO ledger VALUES (1,?)', (json.dumps(state, allow_nan=False),))
            return result

    @staticmethod
    def equity(state):
        reserved = sum(o['reserved'] for o in state.get('pending', {}).values() if o['side'] == 'buy')
        return state['cash'] + reserved + sum(p['qty'] * state['prices'].get(s, p['entry_price'])
                                              for s, p in state['positions'].items())

    def limits(self, state, now):
        equity = self.equity(state)
        if self.live and not state.get('synced'):
            return equity
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
                          note=('LIVE: real money. Cash is synced from the exchange; values are USDT, assumed USD-equivalent.'
                                if self.live else
                                'PAPER ONLY; values are USDT, assumed USD-equivalent; marks may be stale.'))
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

    def news_block(self, symbol, hours, reason, now):
        if not self.config.get('news'):
            raise ValueError('News veto is off. Use option C with news true in config.json.')
        if symbol != '*' and symbol not in self.config['symbols']:
            raise ValueError('Symbol must be an allowed symbol or * for all.')
        if not finite(hours) or not 1 <= hours <= 168:
            raise ValueError('Hours must be from 1 to 168.')
        reason = reason.strip()
        if not 1 <= len(reason) <= 200:
            raise ValueError('A reason of 1 to 200 characters is required.')
        def operation(state):
            blocks = [b for b in state.setdefault('news_blocks', []) if b['until'] > now]
            if len(blocks) >= 50:
                raise ValueError('Too many active blocks; clear some first.')
            until = now+hours*3600
            blocks.append(dict(symbol=symbol, created=now, until=until, reason=reason))
            state['news_blocks'] = blocks
            self.event(state, now, kind='news_block', symbol=symbol, until=until, reason=reason)
            return dict(symbol=symbol, until=until, reason=reason, active_blocks=len(blocks))
        return self.transact(now, operation)

    def news_clear(self, symbol, note, now):
        if not note.strip():
            raise ValueError('A written note is required to remove a veto.')
        def operation(state):
            blocks = state.setdefault('news_blocks', [])
            kept = [b for b in blocks if b['symbol'] != symbol and b['until'] > now]
            removed = len([b for b in blocks if b['symbol'] == symbol and b['until'] > now])
            state['news_blocks'] = kept
            self.event(state, now, kind='news_clear', symbol=symbol, note=note.strip(), removed=removed)
            return dict(symbol=symbol, removed=removed, active_blocks=len(kept))
        return self.transact(now, operation)

    def news_list(self, now):
        blocks = self.status(now).get('news_blocks', [])
        return dict(news_veto_enabled=bool(self.config.get('news')),
                    active_blocks=[b for b in blocks if b['until'] > now])

    def _note_exit_failure(self, state, symbol, now):
        fails = state.setdefault('exit_fails', {}).get(symbol, 0)+1
        state['exit_fails'][symbol] = fails
        state.setdefault('exit_wait', {})[symbol] = now+min(600, 15*2**fails)
        if fails == 5:
            state.update(paused=True, review_required=True)
            self.event(state, now, kind='live_halt', reason=f'{symbol} exit failed {fails} times')

    def _begin_exit(self, state, symbol, price, now, reason):
        pending = state.setdefault('pending', {})
        state.setdefault('exit_wanted', {}).setdefault(symbol, reason)
        if any(o['symbol'] == symbol and o['side'] == 'sell' for o in pending.values()):
            return
        if now < state.get('exit_wait', {}).get(symbol, 0):
            return
        cid = order_id('x', f'{symbol}:{int(now*1000)}:{len(state["events"])}')
        pending[cid] = dict(side='sell', symbol=symbol, qty=state['positions'][symbol]['qty'], reason=reason,
                            ref_price=price, created=now, submitted=None)
        self.event(state, now, kind='live_exit_intent', symbol=symbol, reason=reason, client_order_id=cid)

    def _sell(self, state, symbol, price, now, reason):
        if self.live:
            return self._begin_exit(state, symbol, price, now, reason)
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
                if self.live and symbol in state.get('exit_wanted', {}):
                    self._sell(state, symbol, price, now, state['exit_wanted'][symbol])
                elif state['review_required'] or price <= p['stop'] or price >= p['take_profit'] or p['qty']*price > cap:
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
            if action not in ('buy', 'sell') or source not in ('tradingview', 'scanner', 'copy', 'demo', 'backtest'):
                return reject('unsupported_signal_source_or_action')
            if source == 'scanner' and self.config['option'] not in ('B', 'C'):
                return reject('scanner_disabled')
            if source == 'copy' and not self.config.get('copy_trading'):
                return reject('copy_disabled')
            if self.live and source in ('demo', 'backtest'):
                return reject('simulated_source_not_allowed_live')
            if not finite(price) or price <= 0 or not finite(atr) or atr <= 0:
                return reject('invalid_price_or_atr')
            if source == 'copy':
                if source_time is None:
                    return reject('copy_requires_source_time')
                if not finite(source_time) or not 0 <= now-source_time <= 120:
                    return reject('stale_or_future_signal')
            state['prices'][symbol] = price
            equity = self.limits(state, now)
            if action == 'sell':
                if symbol not in state['positions']:
                    buying = [(cid, o) for cid, o in state.get('pending', {}).items()
                              if self.live and o['symbol'] == symbol and o['side'] == 'buy']
                    if not buying:
                        return reject('no_bot_position')
                    cid, order = buying[0]
                    if order['submitted'] is None:
                        del state['pending'][cid]
                        state['cash'] += order['reserved']
                        self.event(state, now, kind='live_released', client_order_id=cid, symbol=symbol,
                                   side='buy', reason='sell signal before submission')
                        return dict(accepted=True, mode='live', action='sell', cancelled_pending_buy=True)
                    state.setdefault('exit_wanted', {})[symbol] = 'signal'
                    self.event(state, now, kind='live_exit_deferred', symbol=symbol, id=signal_id)
                    return dict(accepted=True, mode='live', action='sell', deferred=True)
                self._sell(state, symbol, price, now, 'signal')
                self.limits(state, now)
                return dict(accepted=True, mode='live' if self.live else 'paper', action='sell')
            if state['paused'] or state['killed'] or state['review_required']:
                return reject('safety_halt')
            if self.config.get('news') and any(b['symbol'] in (symbol, '*') and b['until'] > now
                                                for b in state.get('news_blocks', [])):
                return reject('news_veto')
            buying = {o['symbol']: o for o in state.get('pending', {}).values() if o['side'] == 'buy'}
            if symbol in state['positions'] or symbol in buying:
                return reject('position_already_open')
            if len(state['positions'])+len(buying) >= min(5, int(self.config['max_positions'])):
                return reject('max_positions')
            if self.live and not state.get('synced'):
                return reject('not_synced_with_exchange')
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
            allocated = (sum(p['cost'] for p in state['positions'].values())
                         + sum(o['reserved'] for o in buying.values()))
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
            if self.live:
                quote = math.floor(qty*fill_price*100)/100
                if quote < max(1.0, liquidity['min_notional']):
                    return reject('order_too_small')
                cid = order_id('b', signal_id)
                state.get('exit_wanted', {}).pop(symbol, None)
                state['cash'] -= cost
                state['pending'][cid] = dict(side='buy', symbol=symbol, quote=quote, reserved=cost, stop=stop,
                    take_profit=price+4*atr, source=source, strategy=strategy, signal_id=signal_id,
                    ref_price=price, created=now, submitted=None,
                    latency=None if source_time is None else now-source_time)
                self.event(state, now, kind='live_entry_intent', id=signal_id, symbol=symbol, quote=quote,
                           source=source, strategy=strategy, client_order_id=cid)
                return dict(accepted=True, mode='live', action='buy', pending=True, client_order_id=cid,
                            quote_order_qty=quote, cost=cost, qty=qty)
            state['cash'] -= cost
            state['positions'][symbol] = dict(qty=qty, entry_price=fill_price, cost=cost,
                stop=stop, take_profit=price+4*atr, source=source, strategy=strategy)
            self.event(state, now, kind='fill', action='buy', id=signal_id, symbol=symbol,
                price=fill_price, qty=qty, fee=qty*fill_price*fee, source=source, strategy=strategy,
                latency=None if source_time is None else now-source_time)
            self.limits(state, now)
            return dict(accepted=True, mode='paper', action='buy', cost=cost, qty=qty)
        return self.transact(now, operation)

    def auto_block(self, symbol, reason, now):
        if not self.config.get('news'):
            return False
        def operation(state):
            blocks = [b for b in state.setdefault('news_blocks', []) if b['until'] > now]
            state['news_blocks'] = blocks
            if any(b['symbol'] == symbol and b.get('auto') and b['until'] > now+1800 for b in blocks):
                return False
            blocks.append(dict(symbol=symbol, created=now, until=now+3600, reason='AUTO: '+reason[:190], auto=True))
            self.event(state, now, kind='news_block', symbol=symbol, until=now+3600, reason='AUTO: '+reason[:190])
            return True
        return self.transact(now, operation)

    def live_outbox(self, now):
        return self.transact(now, lambda state: copy.deepcopy(state.get('pending', {})))

    @staticmethod
    def _fill_time(order, executed_at, now):
        if finite(executed_at) and (order.get('submitted') or 0)-60 <= executed_at <= now+60:
            return executed_at
        return now

    def _pending(self, state, cid, side):
        order = state.get('pending', {}).get(cid)
        if order is None or order['side'] != side:
            raise ValueError('Unknown pending order.')
        return state['pending'].pop(cid)

    def live_submitted(self, cid, now):
        def operation(state):
            order = state.get('pending', {}).get(cid)
            if order is None:
                raise ValueError('Unknown pending order.')
            order['submitted'] = now
            self.event(state, now, kind='live_submitted', client_order_id=cid, symbol=order['symbol'], side=order['side'])
        return self.transact(now, operation)

    def live_release(self, cid, reason, now):
        def operation(state):
            order = state.get('pending', {}).pop(cid, None)
            if order is None:
                raise ValueError('Unknown pending order.')
            if order['side'] == 'buy':
                state['cash'] += order['reserved']
                state.get('exit_wanted', {}).pop(order['symbol'], None)
            else:
                self._note_exit_failure(state, order['symbol'], now)
            self.event(state, now, kind='live_released', client_order_id=cid, symbol=order['symbol'],
                       side=order['side'], reason=reason[:200])
            self.limits(state, now)
        return self.transact(now, operation)

    def live_settle_buy(self, cid, qty, quote_spent, fee_value, fee_cash, now, executed_at=None,
                        base_fee_value=0.0, exec_price=None):
        def operation(state):
            order = self._pending(state, cid, 'buy')
            state['cash'] += order['reserved']
            symbol = order['symbol']
            if not (finite(qty) and finite(quote_spent) and qty > 0 and quote_spent > 0):
                state.get('exit_wanted', {}).pop(symbol, None)
                self.event(state, now, kind='live_released', client_order_id=cid, symbol=symbol,
                           side='buy', reason='nothing filled')
                return dict(filled=False)
            state['cash'] -= quote_spent+fee_cash
            state['positions'][symbol] = dict(qty=qty, entry_price=quote_spent/qty, cost=quote_spent+fee_value,
                stop=order['stop'], take_profit=order['take_profit'], source=order['source'],
                strategy=order['strategy'])
            self.event(state, self._fill_time(order, executed_at, now), kind='fill', action='buy', id=order['signal_id'], symbol=symbol,
                price=exec_price or quote_spent/qty, qty=qty, fee=fee_value+base_fee_value, source=order['source'],
                strategy=order['strategy'], latency=order['latency'], client_order_id=cid, mode='live')
            self.limits(state, now)
            return dict(filled=True, qty=qty)
        return self.transact(now, operation)

    def live_settle_sell(self, cid, qty, quote_received, fee_value, fee_cash, now, executed_at=None):
        def operation(state):
            order = self._pending(state, cid, 'sell')
            symbol = order['symbol']
            position = state['positions'].get(symbol)
            if position is None or not (finite(qty) and finite(quote_received) and qty > 0 and quote_received > 0):
                if position is not None:
                    self._note_exit_failure(state, symbol, now)
                self.event(state, now, kind='live_released', client_order_id=cid, symbol=symbol,
                           side='sell', reason='nothing sold')
                return dict(filled=False)
            remaining = position['qty']-qty
            last = state['prices'].get(symbol, quote_received/qty)
            closing = remaining*last < 1.0
            cost_part = position['cost'] if closing else position['cost']*qty/position['qty']
            proceeds = quote_received-fee_value
            state['cash'] += quote_received-fee_cash
            state.get('exit_fails', {}).pop(symbol, None)
            state.get('exit_wait', {}).pop(symbol, None)
            self.event(state, self._fill_time(order, executed_at, now), kind='fill', action='sell', symbol=symbol,
                strategy=position['strategy'], source=position['source'], qty=qty, price=quote_received/qty, fee=fee_value,
                pnl=proceeds-cost_part, reason=order['reason'], client_order_id=cid, mode='live')
            if closing:
                del state['positions'][symbol]
                state.get('exit_wanted', {}).pop(symbol, None)
            else:
                position['qty'], position['cost'] = remaining, position['cost']-cost_part
                self._begin_exit(state, symbol, quote_received/qty, now, order['reason'])
            self.limits(state, now)
            return dict(filled=True, qty=qty, closed=closing)
        return self.transact(now, operation)

    def live_write_off_dust(self, symbol, now):
        def operation(state):
            position = state['positions'].pop(symbol, None)
            state.get('exit_wanted', {}).pop(symbol, None)
            if position is None:
                return False
            self.event(state, now, kind='dust_written_off', symbol=symbol, qty=position['qty'],
                       cost=position['cost'], reason='below exchange minimum order; cannot be sold')
            return True
        return self.transact(now, operation)

    def live_acquire(self, token, now):
        def operation(state):
            lock = state.get('lock')
            if lock and lock['token'] != token and now-lock['heartbeat'] < 30:
                raise ValueError('Another live process is already running on this ledger.')
            state['lock'] = dict(token=token, heartbeat=now)
        return self.transact(now, operation)

    def live_halt(self, reason, now):
        def operation(state):
            state.update(paused=True, review_required=True)
            self.event(state, now, kind='live_halt', reason=reason[:300])
        return self.transact(now, operation)

    def live_sync(self, usdt_total, problems, now):
        def operation(state):
            if not state.get('pending'):
                state['cash'] = usdt_total
            if not state.get('synced'):
                state.update(synced=True, day=utc_day(now), day_start=usdt_total)
            equity = self.limits(state, now)
            if problems and not state['review_required']:
                state.update(paused=True, review_required=True)
                self.event(state, now, kind='live_halt', reason='; '.join(problems)[:300])
            return equity
        return self.transact(now, operation)
