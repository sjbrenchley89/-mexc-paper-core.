"""Live executor and go-live checks. Orders come only from ledger intents; every settlement uses exchange records."""
import math
import os
from decimal import ROUND_DOWN, Decimal

import exchange
from risk import FLOOR, TOTAL_CAP

QUOTE = 'USDT'
MAX_ADVERSE_MOVE = 0.005
INTENT_MAX_AGE = 120
NOT_FOUND_GRACE = 15
UNRESOLVED_HALT = 120
WORKING_CANCEL = 60
ACCOUNT_TOLERANCE = 5.0
DUST_VALUE = 1.0
MIN_PAPER_DAYS = 30
MIN_PAPER_TRADES = 50
CLOCK_SKEW_SECONDS = 3
REQUIRED_PERMISSIONS = {'SPOT_ACCOUNT_READ', 'SPOT_DEAL_READ', 'SPOT_DEAL_WRITE'}
FORBIDDEN_PERMISSION_WORDS = ('WITHDRAW', 'TRANSFER', 'CONTRACT')
PHRASE = 'I ACCEPT LIVE TRADING WITH REAL MONEY UP TO 50 USD'


class ProcessLock:
    """Held until the process ends; a stalled but alive process keeps it, so nothing can take over."""

    def __init__(self, path):
        self.path, self.handle = str(path), None

    def acquire(self):
        handle = open(self.path, 'a+b')
        try:
            if os.name == 'nt':
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise ValueError('Another live process is already running on this computer.') from None
        self.handle = handle

    def release(self):
        if self.handle:
            self.handle.close()
            self.handle = None


def number(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return 0.0
    return result if math.isfinite(result) else 0.0


def floor_to(value, step):
    if step <= 0:
        return value
    unit = Decimal(str(step))
    return float((Decimal(str(value))/unit).to_integral_value(ROUND_DOWN)*unit)


def not_found(exc):
    text = str(exc).lower()
    return 'not exist' in text or 'not found' in text or 'unknown order' in text or exc.code in (-2013, 700004)


def balances_of(account):
    return {b['asset']: number(b.get('free'))+number(b.get('locked')) for b in account.get('balances', [])
            if number(b.get('free'))+number(b.get('locked')) > 0}


def paper_trial(state, now):
    fills = [e for e in state['events'] if e['kind'] == 'fill']
    sells = [e for e in fills if e['action'] == 'sell']
    days = (now-fills[0]['timestamp'])/86400 if fills else 0.0
    return dict(days=days, closed_trades=len(sells), net_pnl=sum(e['pnl'] for e in sells),
                halted=bool(state['review_required']))


def doctor(config, paper_state, now, has_secret, has_keys, python_version):
    trial = paper_trial(paper_state, now)
    confirmations = config.get('live', {})
    steps = [
        ('Python 3.12 or newer', python_version >= (3, 12), 'Install Python 3.12 or newer from python.org.'),
        ('Webhook secret created', has_secret, 'Type: python bot.py setup'),
        (f'Paper trading for {MIN_PAPER_DAYS} days', trial['days'] >= MIN_PAPER_DAYS,
         f"{trial['days']:.1f} of {MIN_PAPER_DAYS} days so far. Keep python bot.py start running."),
        (f'{MIN_PAPER_TRADES} closed paper trades', trial['closed_trades'] >= MIN_PAPER_TRADES,
         f"{trial['closed_trades']} of {MIN_PAPER_TRADES} so far. Trades only happen when a signal arrives."),
        ('Paper result positive after costs', trial['net_pnl'] > 0,
         f"{trial['net_pnl']:.2f} USDT so far. If this stays negative, do not go live."),
        ('No paper safety halt', not trial['halted'], 'Review and re-arm: python bot.py review-rearm --note "your reason"'),
        ('MEXC eligibility confirmed in writing',
         confirmations.get('mexc_eligibility_confirmed') is True,
         'Ask MEXC support, then set live.mexc_eligibility_confirmed to true in config.json only if they said yes.'),
        ('Trade-only MEXC key created', confirmations.get('trade_only_key_confirmed') is True,
         'Follow step 15 of START-HERE.md, then set live.trade_only_key_confirmed to true.'),
        ('MEXC_API_KEY and MEXC_API_SECRET set', has_keys, 'Set both environment variables as in step 15.'),
    ]
    rows = [dict(name=n, ok=bool(ok), action=None if ok else a) for n, ok, a in steps]
    pending = [r for r in rows if not r['ok']]
    return dict(steps=rows, ready_for_preflight=not pending,
                next_step=pending[0]['action'] if pending else 'Type: python bot.py live-preflight',
                note='This checks only your computer and settings. It makes no contact with MEXC and trades nothing.')


def preflight(config, client, market, paper_state, now, env_has_keys):
    checks = []
    def add(name, ok, detail, warn=False):
        checks.append(dict(name=name, ok=ok, detail=detail, warning=warn and not ok))
    confirmations = config.get('live', {})
    add('MEXC eligibility confirmed by you', confirmations.get('mexc_eligibility_confirmed') is True,
        'Set live.mexc_eligibility_confirmed to true in config.json only after MEXC support confirms in writing '
        'that an Australian individual may use automated API trading.')
    add('Trade-only key confirmed by you', confirmations.get('trade_only_key_confirmed') is True,
        'Set live.trade_only_key_confirmed to true only if the key has spot trading and reading, and no withdrawals.')
    trial = paper_trial(paper_state, now)
    add('Paper trial length', trial['days'] >= MIN_PAPER_DAYS, f"{trial['days']:.1f} days; need {MIN_PAPER_DAYS}.")
    add('Paper trial trade count', trial['closed_trades'] >= MIN_PAPER_TRADES,
        f"{trial['closed_trades']} closed trades; need {MIN_PAPER_TRADES}.")
    add('Paper trial net result after costs', trial['net_pnl'] > 0, f"{trial['net_pnl']:.2f} USDT.")
    add('Paper account not halted', not trial['halted'], 'A safety halt is active in the paper account.')
    add('API keys present in environment', env_has_keys, 'Set MEXC_API_KEY and MEXC_API_SECRET in your environment.')
    if not env_has_keys or client is None:
        return checks, False
    try:
        skew = abs(client.server_time()/1000-now)
        add('Clock matches MEXC', skew <= CLOCK_SKEW_SECONDS, f'{skew:.1f} seconds apart; need {CLOCK_SKEW_SECONDS} or less.')
        info = client.api_key_info()
        permissions = {p.strip() for p in str(info.get('permissions', '')).split(',') if p.strip()}
        add('API key is valid', info.get('status') == 'VALID', f"status {info.get('status')}.")
        add('API key has the needed permissions', REQUIRED_PERMISSIONS <= permissions,
            'Needs ' + ', '.join(sorted(REQUIRED_PERMISSIONS-permissions)) + '.' if REQUIRED_PERMISSIONS-permissions else 'ok')
        extra = sorted(p for p in permissions if any(w in p.upper() for w in FORBIDDEN_PERMISSION_WORDS))
        add('API key has no withdrawal, transfer or futures permission', not extra,
            'Remove: ' + ', '.join(extra) + '.' if extra else 'ok')
        add('API key is bound to your IP address', bool(str(info.get('ipWhiteList', '')).strip()),
            'No IP whitelist is set; bind the key to your bot machine.', warn=True)
        account = client.account()
        add('Account can trade (spot)', account.get('canTrade') is not False and account.get('accountType', 'SPOT') == 'SPOT',
            f"canTrade={account.get('canTrade')}, accountType={account.get('accountType')}.")
        held = balances_of(account)
        usdt = held.pop(QUOTE, 0.0)
        allowed = {s[:-len(QUOTE)] for s in config['symbols']}
        value, foreign = usdt, []
        for asset, amount in held.items():
            try:
                worth = amount*market.price(asset+QUOTE)
            except Exception:
                worth = None
            if worth is not None and asset in allowed:
                value += worth
            elif worth is None or worth >= DUST_VALUE:
                foreign.append(asset)
        add('Account holds only USDT and allowed coins', not foreign,
            'Other holdings found: ' + ', '.join(sorted(foreign)) + '. Use a separate account holding only the bot budget.' if foreign else 'ok')
        add('Account value within the $50 budget', FLOOR <= value <= TOTAL_CAP+ACCOUNT_TOLERANCE,
            f'About {value:.2f} USDT; must be between {FLOOR:.0f} and {TOTAL_CAP+ACCOUNT_TOLERANCE:.0f}.')
        for symbol in config['symbols']:
            status = market.trading_status(symbol)
            add(f'{symbol} is online for spot trading', status['ok'], f"exchange status {status['status']}.")
        client.test_order(config['symbols'][0], 'BUY', 'MARKET', 'preflighttest', quote_order_qty=5)
        add('Signed test order accepted (nothing was traded)', True, 'ok')
    except exchange.RateLimited as exc:
        add('MEXC checks', False, f'Rate limited; wait {exc.retry_after:.0f} seconds and retry.')
    except exchange.ExchangeError as exc:
        add('MEXC checks', False, str(exc))
    except exchange.Uncertain as exc:
        add('MEXC checks', False, str(exc))
    except (ValueError, KeyError) as exc:
        add('MEXC checks', False, f'Unexpected reply or data: {type(exc).__name__}.')
    ready = all(c['ok'] or c['warning'] for c in checks)
    return checks, ready


class Executor:
    def __init__(self, ledger, client, config, market, log=print):
        self.ledger, self.client, self.config, self.market, self.log = ledger, client, config, market, log
        self.blocked_until = 0.0
        self.token = os.urandom(8).hex()
        self.stopped = False

    def cycle(self, now):
        try:
            self.ledger.live_acquire(self.token, now)
        except ValueError as exc:
            self.stopped = True
            self.log({'live': 'stopped', 'reason': str(exc)})
            return 'stopped'
        if now < self.blocked_until:
            return 'rate_limited'
        try:
            self.sync(now)
            self.mark_positions(now)
            self.run_outbox(now)
        except exchange.RateLimited as exc:
            self.blocked_until = now+exc.retry_after
            self.log({'live': 'rate_limited', 'wait_seconds': exc.retry_after})
            return 'rate_limited'
        except (exchange.ExchangeError, exchange.Uncertain) as exc:
            self.log({'live': 'exchange_error', 'type': type(exc).__name__, 'detail': str(exc)[:200]})
            return 'error'
        return 'ok'

    def mark_positions(self, now):
        quotes = {}
        for symbol in self.ledger.status(now)['positions']:
            try:
                quotes[symbol] = self.market.price(symbol)
            except Exception:
                continue
        if quotes:
            self.ledger.mark(quotes, now)

    def sync(self, now):
        account = self.client.account()
        held = balances_of(account)
        usdt = held.pop(QUOTE, 0.0)
        state = self.ledger.status(now)
        problems = []
        if account.get('canTrade') is False:
            problems.append('exchange reports trading disabled')
        idle = not state.get('pending')
        allowed = {s[:-len(QUOTE)]: s for s in self.config['symbols']}
        value = usdt
        for asset, amount in held.items():
            symbol = allowed.get(asset)
            try:
                price = self.market.price(symbol or asset+QUOTE)
            except Exception:
                price = None
            if price is None:
                if symbol is None:
                    problems.append(f'unvalued holding {asset}')
                continue
            value += amount*price
            if symbol is None and amount*price >= DUST_VALUE:
                problems.append(f'unrecognised holding {asset}')
            if idle and symbol is not None:
                expected = state['positions'].get(symbol, {}).get('qty', 0.0)
                if amount < expected*0.98:
                    problems.append(f'{asset} balance is below the ledger position')
                elif (amount-expected)*price >= DUST_VALUE:
                    problems.append(f'{asset} balance exceeds the ledger position')
        for symbol, position in state['positions'].items():
            if idle and symbol[:-len(QUOTE)] not in held:
                problems.append(f'{symbol[:-len(QUOTE)]} position missing from the account')
        if value > TOTAL_CAP+ACCOUNT_TOLERANCE:
            problems.append('account value exceeds the $50 budget')
        if problems and not state['review_required']:
            self.log({'live': 'halt', 'reasons': problems})
        self.ledger.live_sync(usdt, sorted(set(problems)), now)

    def run_outbox(self, now):
        for cid, order in sorted(self.ledger.live_outbox(now).items(), key=lambda item: item[1]['created']):
            try:
                if order['submitted'] is None:
                    if not self._submit(cid, order, now):
                        continue
                    order = self.ledger.live_outbox(now)[cid]
                self._resolve(cid, order, now)
            except exchange.RateLimited:
                raise
            except Exception as exc:
                self.log({'live': 'order_error', 'symbol': order['symbol'], 'type': type(exc).__name__})

    def _submit(self, cid, order, now):
        symbol = order['symbol']
        if order['side'] == 'buy':
            if now-order['created'] > INTENT_MAX_AGE:
                self.ledger.live_release(cid, 'entry signal expired before submission', now)
                return False
            state = self.ledger.status(now)
            if state['paused'] or state['killed'] or state['review_required']:
                self.ledger.live_release(cid, 'trading was halted before submission', now)
                return False
            if any(b['symbol'] in (symbol, '*') and b['until'] > now for b in state.get('news_blocks', [])):
                self.ledger.live_release(cid, 'news veto began before submission', now)
                return False
            price = self.market.price(symbol)
            if price > order['ref_price']*(1+MAX_ADVERSE_MOVE):
                self.ledger.live_release(cid, 'price moved up before submission', now)
                return False
            args = dict(quote_order_qty=order['quote'])
        else:
            rules = self.market.symbol_rules_for(symbol)
            base = symbol[:-len(QUOTE)]
            free = balances_of(self.client.account()).get(base, 0.0)
            quantity = floor_to(min(order['qty'], free), rules['step'])
            price = self.market.price(symbol)
            if quantity < rules['min_qty'] or quantity*price < rules['min_notional']:
                self.ledger.live_release(cid, 'remaining quantity is below the exchange minimum', now)
                self.ledger.live_write_off_dust(symbol, now)
                self.log({'live': 'dust_written_off', 'symbol': symbol})
                return False
            args = dict(quantity=quantity)
        self.ledger.live_submitted(cid, now)
        try:
            self.client.new_order(symbol, order['side'].upper(), 'MARKET', cid, **args)
        except exchange.Uncertain:
            pass
        except exchange.RateLimited:
            raise
        except exchange.ExchangeError as exc:
            self.ledger.live_release(cid, f'rejected: {exc}', now)
            self.log({'live': 'order_rejected', 'symbol': symbol, 'side': order['side'], 'detail': str(exc)[:200]})
            return False
        return True

    def _resolve(self, cid, order, now):
        symbol, waited = order['symbol'], now-order['submitted']
        try:
            found = self.client.query_order(symbol, cid)
        except exchange.RateLimited:
            raise
        except exchange.ExchangeError as exc:
            if not_found(exc):
                if waited >= NOT_FOUND_GRACE:
                    self.ledger.live_release(cid, 'order never reached the exchange', now)
                return
            return self._unresolved(cid, waited, now, str(exc)[:200])
        except exchange.Uncertain as exc:
            return self._unresolved(cid, waited, now, str(exc))
        status = found.get('status')
        if status in ('NEW', 'PARTIALLY_FILLED'):
            if waited > WORKING_CANCEL:
                try:
                    self.client.cancel_order(symbol, cid)
                except exchange.RateLimited:
                    raise
                except (exchange.ExchangeError, exchange.Uncertain):
                    self._unresolved(cid, waited, now, 'could not cancel a working order')
            return
        if status not in ('FILLED', 'PARTIALLY_CANCELED', 'CANCELED'):
            return self._unresolved(cid, waited, now, f'unrecognised order status {status}')
        self._settle(cid, order, found, now)

    def _unresolved(self, cid, waited, now, detail):
        if waited >= UNRESOLVED_HALT:
            self.ledger.live_halt(f'order {cid[:8]} unresolved after {waited:.0f}s: {detail}', now)
            self.log({'live': 'halt', 'reasons': ['unresolved order']})

    def _fees(self, order, found, now):
        symbol, base = order['symbol'], order['symbol'][:-len(QUOTE)]
        quote_amount = number(found.get('cumulativeQuoteQty', found.get('cummulativeQuoteQty')))
        try:
            trades = self.client.my_trades(symbol, found.get('orderId'))
        except exchange.RateLimited:
            raise
        except (exchange.ExchangeError, exchange.Uncertain):
            trades = []
        if not isinstance(trades, list) or not trades:
            estimate = quote_amount*float(self.config['fee'])
            return dict(value=estimate, cash=estimate, base=0.0, estimated=True, time=None)
        value = cash = base_fee = 0.0
        estimated = False
        times = [number(t.get('time'))/1000 for t in trades if number(t.get('time')) > 0]
        for trade in trades:
            amount, asset = number(trade.get('commission')), trade.get('commissionAsset')
            if asset == QUOTE:
                value += amount
                cash += amount
            elif asset == base:
                base_fee += amount
            else:
                value += number(trade.get('quoteQty'))*float(self.config['fee'])
                estimated = True
        return dict(value=value, cash=cash, base=base_fee, estimated=estimated, time=max(times) if times else None)

    def _settle(self, cid, order, found, now):
        executed = number(found.get('executedQty'))
        quote_amount = number(found.get('cumulativeQuoteQty', found.get('cummulativeQuoteQty')))
        fees = self._fees(order, found, now)
        stamp = number(found.get('updateTime') or found.get('time'))/1000 or None
        when = fees['time'] or stamp
        if order['side'] == 'buy':
            exec_price = quote_amount/executed if executed > 0 else None
            result = self.ledger.live_settle_buy(cid, executed-fees['base'], quote_amount, fees['value'], fees['cash'],
                                                 now, when, fees['base']*(exec_price or 0.0), exec_price)
        else:
            result = self.ledger.live_settle_sell(cid, executed, quote_amount, fees['value'], fees['cash'], now, when)
        self.log({'live': 'settled', 'symbol': order['symbol'], 'side': order['side'], 'filled': result['filled'],
                  'fee_estimated': fees['estimated']})
