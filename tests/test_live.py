import copy
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import patch

import bot
import exchange
import live
from risk import Ledger

NOW = 1700000000
CONFIG = json.loads((bot.ROOT/'config.json').read_text())
LIQUIDITY = dict(spread=.001, depth=5000, step=.0001, min_qty=.0001, min_notional=1)


class FakeMarket:
    def __init__(self):
        self.last = 100.0
        self.step = .0001

    def price(self, symbol):
        if symbol not in ('BTCUSDT',):
            raise RuntimeError('unknown symbol')
        return self.last

    def symbol_rules_for(self, symbol):
        return dict(step=self.step, min_qty=self.step, min_notional=1.0)

    def trading_status(self, symbol):
        return dict(ok=True, status='1')


class FakeClient:
    """Behaves like the documented MEXC endpoints; records every order it receives."""

    def __init__(self, market, usdt=50.0):
        self.market, self.balances = market, {'USDT': usdt}
        self.orders, self.trades, self.sent, self.tests = {}, {}, [], []
        self.fee_asset, self.fee_rate = 'USDT', .0005
        self.on_new = None
        self.permissions = 'SPOT_ACCOUNT_READ,SPOT_DEAL_READ,SPOT_DEAL_WRITE'
        self.ip = '1.2.3.4'
        self.can_trade = True
        self.skew = 0
        self.now_ms = None
        self.partial_sell = None

    def server_time(self):
        return int((NOW+self.skew)*1000)

    def api_key_info(self):
        return dict(status='VALID', permissions=self.permissions, ipWhiteList=self.ip)

    def account(self):
        return dict(canTrade=self.can_trade, accountType='SPOT',
                    balances=[dict(asset=a, free=str(v), locked='0') for a, v in self.balances.items()])

    def test_order(self, *args, **kwargs):
        self.tests.append((args, kwargs))
        return {}

    def new_order(self, symbol, side, order_type, cid, quantity=None, quote_order_qty=None):
        self.sent.append(dict(symbol=symbol, side=side, type=order_type, cid=cid, quantity=quantity,
                              quote=quote_order_qty))
        if self.on_new:
            behaviour = self.on_new
            if behaviour == 'reject':
                raise exchange.ExchangeError('MEXC rejected the request (HTTP 400, code 30004): insufficient', code=30004, status=400)
            if behaviour == 'ratelimit':
                raise exchange.RateLimited(30)
            if behaviour == 'lost':
                raise exchange.Uncertain('timeout')
        price = self.market.last
        if side == 'BUY':
            qty, quote = quote_order_qty/price, quote_order_qty
        else:
            qty = quantity*(self.partial_sell or 1)
            quote = qty*price
            if qty > self.balances.get('BTC', 0)+1e-12:
                raise exchange.ExchangeError('Insufficient position', code=30004, status=400)
        commission = {'USDT': quote*self.fee_rate, 'BTC': qty*self.fee_rate, 'MX': 3.0}[self.fee_asset]
        if side == 'BUY':
            self.balances['USDT'] -= quote
            self.balances['BTC'] = self.balances.get('BTC', 0)+qty
            if self.fee_asset == 'USDT':
                self.balances['USDT'] -= commission
            elif self.fee_asset == 'BTC':
                self.balances['BTC'] -= commission
        else:
            self.balances['BTC'] -= qty
            self.balances['USDT'] += quote
            if self.fee_asset == 'USDT':
                self.balances['USDT'] -= commission
        oid = f'order{len(self.orders)+1}'
        status = 'PARTIALLY_CANCELED' if side == 'SELL' and self.partial_sell else 'FILLED'
        self.orders[cid] = dict(orderId=oid, status=status, executedQty=str(qty), cumulativeQuoteQty=str(quote))
        self.trades[oid] = [dict(commission=str(commission), commissionAsset=self.fee_asset, quoteQty=str(quote))]
        if self.now_ms:
            self.orders[cid].update(time=self.now_ms, updateTime=self.now_ms)
            self.trades[oid][0]['time'] = self.now_ms
        if behaviour_is(self.on_new, 'executed_then_timeout'):
            raise exchange.Uncertain('timeout after send')
        return dict(orderId=oid)

    def query_order(self, symbol, cid):
        if cid not in self.orders:
            raise exchange.ExchangeError('MEXC rejected the request (HTTP 400, code -2013): Order does not exist',
                                         code=-2013, status=400)
        return dict(self.orders[cid])

    def cancel_order(self, symbol, cid):
        return {}

    def my_trades(self, symbol, order_id):
        return self.trades.get(order_id, [])


def behaviour_is(value, name):
    return value == name


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.config = copy.deepcopy(CONFIG)
        self.market = FakeMarket()
        self.client = FakeClient(self.market)
        self.ledger = Ledger(self.dir/'live.sqlite', self.config, live=True)
        self.logs = []
        self.executor = live.Executor(self.ledger, self.client, self.config, self.market, log=self.logs.append)
        self.now = NOW
        self.executor.cycle(self.now)

    def tearDown(self):
        self.tmp.cleanup()

    def tick(self, seconds=5):
        self.now += seconds
        return self.executor.cycle(self.now)

    def buy(self, identifier='sig1'):
        return self.ledger.signal(identifier, 'BTCUSDT', 'buy', self.market.last, 1.0, 'tradingview',
                                  'technical', self.now, self.now, LIQUIDITY)

    def state(self):
        return self.ledger.status(self.now)


class RoundTripTests(Base):
    def test_first_sync_takes_cash_from_exchange(self):
        state = self.state()
        self.assertEqual((state['mode'], state['cash'], state['day_start'], state['synced']), ('live', 50.0, 50.0, True))

    def test_buy_is_queued_then_filled_and_matches_account(self):
        result = self.buy()
        self.assertTrue(result['pending'])
        self.assertEqual(self.client.sent, [])
        self.assertLessEqual(result['quote_order_qty'], 10)
        self.tick()
        self.assertEqual(len(self.client.sent), 1)
        self.assertEqual(self.client.sent[0]['type'], 'MARKET')
        state = self.state()
        position = state['positions']['BTCUSDT']
        self.assertAlmostEqual(position['qty'], self.client.balances['BTC'])
        self.assertAlmostEqual(state['cash'], self.client.balances['USDT'])
        self.assertEqual(state['pending'], {})
        self.assertFalse(state['review_required'])

    def test_stop_exit_sells_whole_holding_and_books_real_loss(self):
        self.buy()
        self.tick()
        self.market.last = 97.0
        self.ledger.mark({'BTCUSDT': 97.0}, self.now+1)
        self.tick()
        state = self.state()
        self.assertEqual(state['positions'], {})
        self.assertEqual([s['side'] for s in self.client.sent], ['BUY', 'SELL'])
        self.assertAlmostEqual(state['cash'], self.client.balances['USDT'])
        sells = [e for e in state['events'] if e['kind'] == 'fill' and e['action'] == 'sell']
        self.assertEqual(len(sells), 1)
        self.assertLess(sells[0]['pnl'], 0)
        self.assertEqual(sells[0]['reason'], 'stop')

    def test_fee_charged_in_base_coin_does_not_cause_a_mismatch(self):
        self.client.fee_asset = 'BTC'
        self.buy()
        self.tick()
        position = self.state()['positions']['BTCUSDT']
        self.assertAlmostEqual(position['qty'], self.client.balances['BTC'])
        self.tick()
        self.assertFalse(self.state()['review_required'])
        self.market.last = 97.0
        self.ledger.mark({'BTCUSDT': 97.0}, self.now+1)
        self.tick()
        self.assertEqual(self.state()['positions'], {})

    def test_fee_in_other_coin_is_estimated_and_flagged(self):
        self.client.fee_asset = 'MX'
        self.buy()
        self.tick()
        settled = [log for log in self.logs if log.get('live') == 'settled']
        self.assertTrue(settled[-1]['fee_estimated'])
        self.assertAlmostEqual(self.state()['cash'], self.client.balances['USDT'])

    def test_kill_switch_sells_open_position(self):
        self.buy()
        self.tick()
        self.ledger.control('kill', self.now+1)
        self.ledger.mark({'BTCUSDT': 100.0}, self.now+2)
        self.tick()
        self.assertEqual(self.state()['positions'], {})
        self.assertEqual(self.client.sent[-1]['side'], 'SELL')


class SafetyTests(Base):
    def test_second_signal_while_buy_pending_is_refused(self):
        self.buy('a')
        self.assertEqual(self.buy('b')['reason'], 'position_already_open')

    def test_pending_buy_reserves_cash_and_budget(self):
        result = self.buy()
        state = self.state()
        self.assertAlmostEqual(state['cash'], 50-result['cost'])
        self.assertLessEqual(result['cost'], 10)

    def test_rejected_order_restores_cash(self):
        self.client.on_new = 'reject'
        self.buy()
        self.tick()
        state = self.state()
        self.assertEqual((state['cash'], state['positions'], state['pending']), (50.0, {}, {}))

    def test_timeout_after_execution_settles_without_resending(self):
        self.client.on_new = 'executed_then_timeout'
        self.buy()
        self.tick()
        self.assertEqual(len(self.client.sent), 1)
        self.assertIn('BTCUSDT', self.state()['positions'])

    def test_timeout_when_order_never_arrived_releases_after_grace(self):
        self.client.on_new = 'lost'
        self.buy()
        self.tick()
        self.assertEqual(len(self.state()['pending']), 1)
        self.tick(live.NOT_FOUND_GRACE+1)
        state = self.state()
        self.assertEqual((state['cash'], state['positions'], state['pending']), (50.0, {}, {}))
        self.assertEqual(len(self.client.sent), 1)

    def test_unresolvable_order_halts_trading(self):
        self.client.on_new = 'lost'
        self.buy()
        self.tick()
        self.client.query_order = lambda s, c: (_ for _ in ()).throw(exchange.Uncertain('down'))
        self.tick(live.UNRESOLVED_HALT+1)
        self.assertTrue(self.state()['review_required'])

    def test_price_jump_before_submission_cancels_the_buy(self):
        self.buy()
        self.market.last = 101.0
        self.tick()
        self.assertEqual(self.client.sent, [])
        state = self.state()
        self.assertEqual((state['cash'], state['pending']), (50.0, {}))

    def test_rate_limit_pauses_requests(self):
        self.client.on_new = 'ratelimit'
        self.buy()
        self.assertEqual(self.tick(), 'rate_limited')
        calls = len(self.client.sent)
        self.assertEqual(self.tick(5), 'rate_limited')
        self.assertEqual(len(self.client.sent), calls)

    def test_foreign_holdings_halt_trading(self):
        self.client.balances['BTC'] = 0.5
        self.market.last = 100.0
        self.tick()
        self.assertTrue(self.state()['review_required'])
        self.assertEqual(self.buy('late')['reason'], 'safety_halt')

    def test_position_missing_from_account_halts(self):
        self.buy()
        self.tick()
        self.client.balances['BTC'] = 0.0
        self.tick()
        self.assertTrue(self.state()['review_required'])

    def test_oversized_account_halts(self):
        self.client.balances['USDT'] = 500
        self.tick()
        self.assertTrue(self.state()['review_required'])

    def test_halt_event_not_repeated_every_cycle(self):
        self.client.balances['USDT'] = 500
        for _ in range(4):
            self.tick()
        halts = [e for e in self.state()['events'] if e['kind'] == 'live_halt']
        self.assertEqual(len(halts), 1)

    def test_simulated_sources_refused_in_live_ledger(self):
        for source in ('demo', 'backtest'):
            result = self.ledger.signal(source, 'BTCUSDT', 'buy', 100, 1, source, 'technical', self.now)
            self.assertEqual(result['reason'], 'simulated_source_not_allowed_live')

    def test_unsynced_live_ledger_refuses_entries(self):
        fresh = Ledger(self.dir/'other.sqlite', self.config, live=True)
        result = fresh.signal('s', 'BTCUSDT', 'buy', 100, 1, 'tradingview', 'technical', NOW, NOW, LIQUIDITY)
        self.assertEqual(result['reason'], 'not_synced_with_exchange')
        self.assertFalse(fresh.status(NOW)['review_required'])

    def test_news_veto_applies_in_live(self):
        self.ledger.news_block('BTCUSDT', 1, 'delisting notice', self.now)
        self.assertEqual(self.buy()['reason'], 'news_veto')

    def test_dust_position_that_cannot_be_sold_is_written_off(self):
        self.buy()
        self.tick()
        self.market.step = 1000.0
        self.market.last = 97.0
        self.ledger.mark({'BTCUSDT': 97.0}, self.now+1)
        self.tick()
        state = self.state()
        self.assertEqual(state['positions'], {})
        self.assertTrue(any(e['kind'] == 'dust_written_off' for e in state['events']))

    def test_daily_loss_limit_still_applies_to_real_equity(self):
        self.buy()
        self.tick()
        self.market.last = 40.0
        self.ledger.mark({'BTCUSDT': 40.0}, self.now+1)
        self.assertTrue(self.state()['review_required'])


class PreflightTests(Base):
    def paper_state(self, days=40, trades=60, pnl=1.0):
        events = [dict(timestamp=NOW-days*86400, kind='fill', action='buy', symbol='BTCUSDT')]
        events += [dict(timestamp=NOW-86400, kind='fill', action='sell', symbol='BTCUSDT', pnl=pnl/trades)
                   for _ in range(trades)]
        return dict(events=events, review_required=False)

    def run_checks(self, config=None, state=None, client=None, keys=True):
        config = config or copy.deepcopy(self.config)
        config['live'] = dict(mexc_eligibility_confirmed=True, trade_only_key_confirmed=True)
        return live.preflight(config, client or self.client, self.market, state or self.paper_state(), NOW, keys)

    def failures(self, checks):
        return [c['name'] for c in checks if not c['ok'] and not c['warning']]

    def test_everything_good_is_ready_and_places_no_real_order(self):
        checks, ready = self.run_checks()
        self.assertTrue(ready, self.failures(checks))
        self.assertEqual(len(self.client.tests), 1)
        self.assertEqual(self.client.sent, [])

    def test_unconfirmed_eligibility_blocks(self):
        config = copy.deepcopy(self.config)
        checks, ready = live.preflight(config, self.client, self.market, self.paper_state(), NOW, True)
        self.assertFalse(ready)
        self.assertIn('MEXC eligibility confirmed by you', self.failures(checks))

    def test_short_or_thin_or_losing_paper_trial_blocks(self):
        for state, name in ((self.paper_state(days=5), 'Paper trial length'),
                            (self.paper_state(trades=10), 'Paper trial trade count'),
                            (self.paper_state(pnl=-1.0), 'Paper trial net result after costs')):
            checks, ready = self.run_checks(state=state)
            self.assertFalse(ready)
            self.assertIn(name, self.failures(checks))

    def test_active_paper_halt_blocks(self):
        state = self.paper_state()
        state['review_required'] = True
        self.assertFalse(self.run_checks(state=state)[1])

    def test_withdrawal_transfer_or_futures_permission_blocks(self):
        for extra in ('SPOT_WITHDRAW_WRITE', 'SPOT_TRANSFER_READ', 'CONTRACT_DEAL_WRITE'):
            self.client.permissions += ','+extra
            checks, ready = self.run_checks()
            self.assertFalse(ready, extra)
            self.assertIn('API key has no withdrawal, transfer or futures permission', self.failures(checks))
            self.client.permissions = 'SPOT_ACCOUNT_READ,SPOT_DEAL_READ,SPOT_DEAL_WRITE'

    def test_missing_trade_permission_blocks(self):
        self.client.permissions = 'SPOT_ACCOUNT_READ,SPOT_DEAL_READ'
        self.assertFalse(self.run_checks()[1])

    def test_missing_ip_binding_only_warns(self):
        self.client.ip = ''
        checks, ready = self.run_checks()
        self.assertTrue(ready)
        self.assertTrue(any(c['warning'] for c in checks))

    def test_other_holdings_or_oversized_or_undersized_account_block(self):
        self.client.balances['ETH'] = 1.0
        self.assertFalse(self.run_checks()[1])
        del self.client.balances['ETH']
        self.client.balances['USDT'] = 500
        self.assertFalse(self.run_checks()[1])
        self.client.balances['USDT'] = 10
        self.assertFalse(self.run_checks()[1])

    def test_missing_keys_stop_before_any_exchange_call(self):
        checks, ready = self.run_checks(client=None, keys=False)
        self.assertFalse(ready)
        self.assertEqual(self.client.tests, [])

    def test_clock_skew_blocks(self):
        self.client.skew = 30
        self.assertFalse(self.run_checks()[1])

    def test_rejected_test_order_blocks(self):
        def reject(*args, **kwargs):
            raise exchange.ExchangeError('MEXC rejected the request (HTTP 403, code 700002): bad signature', code=700002, status=403)
        self.client.test_order = reject
        self.assertFalse(self.run_checks()[1])

    def test_paper_trial_summary(self):
        trial = live.paper_trial(self.paper_state(days=31, trades=3, pnl=0.3), NOW)
        self.assertAlmostEqual(trial['days'], 31)
        self.assertEqual(trial['closed_trades'], 3)


class ClientTests(unittest.TestCase):
    def make(self, responder, clock=lambda: 1700000000.0, synced=True):
        seen = []
        def opener(request, timeout):
            seen.append(request)
            return responder(request)
        client = exchange.MexcClient('KEY123', 'SECRET456', opener=opener, clock=clock)
        if synced:
            client._synced = clock()
        return client, seen

    @staticmethod
    def stamp(request):
        return int(urllib.parse.parse_qs(urllib.parse.urlparse(request.full_url).query)['timestamp'][0])

    class Reply(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False

    def test_signature_covers_exact_query_and_secret_never_sent(self):
        import hashlib
        import hmac
        client, seen = self.make(lambda r: self.Reply(b'{"orderId":"1"}'))
        client.new_order('BTCUSDT', 'BUY', 'MARKET', 'cid1', quote_order_qty=9.48)
        request = seen[0]
        url = request.full_url
        self.assertEqual(request.get_method(), 'POST')
        self.assertEqual(request.data, b'')
        self.assertEqual(request.get_header('X-mexc-apikey'), 'KEY123')
        base, signature = url.rsplit('&signature=', 1)
        query = base.split('?', 1)[1]
        self.assertEqual(signature, hmac.new(b'SECRET456', query.encode(), hashlib.sha256).hexdigest())
        self.assertIn('symbol=BTCUSDT&side=BUY&type=MARKET&quoteOrderQty=9.48&newClientOrderId=cid1', query)
        self.assertIn('recvWindow=5000&timestamp=1700000000000', query)
        self.assertNotIn('SECRET456', url)
        self.assertNotIn('SECRET456', repr(client))

    def test_number_formatting_has_no_exponent_or_noise(self):
        self.assertEqual(exchange.fmt(0.0000001), '0.0000001')
        self.assertEqual(exchange.fmt(9.5), '9.5')
        self.assertEqual(exchange.fmt(10.0), '10')

    def test_exactly_one_quantity_form(self):
        client, _ = self.make(lambda r: self.Reply(b'{}'))
        with self.assertRaises(ValueError):
            client.new_order('BTCUSDT', 'BUY', 'MARKET', 'c')
        with self.assertRaises(ValueError):
            client.new_order('BTCUSDT', 'BUY', 'MARKET', 'c', quantity=1, quote_order_qty=1)

    def test_public_call_has_no_key_or_signature(self):
        client, seen = self.make(lambda r: self.Reply(b'{"serverTime":5}'))
        self.assertEqual(client.server_time(), 5)
        self.assertNotIn('signature', seen[0].full_url)
        self.assertIsNone(seen[0].get_header('X-mexc-apikey'))

    def http_error(self, status, body=b'', headers=None):
        def responder(request):
            raise urllib.error.HTTPError(request.full_url, status, 'x', headers or {}, io.BytesIO(body))
        return responder

    def test_error_classes(self):
        client, _ = self.make(self.http_error(400, b'{"code":30004,"msg":"Insufficient position"}'))
        with self.assertRaises(exchange.ExchangeError) as caught:
            client.account()
        self.assertEqual(caught.exception.code, 30004)
        self.assertNotIn('KEY123', str(caught.exception))
        client, _ = self.make(self.http_error(429, b'', {'Retry-After': '120'}))
        with self.assertRaises(exchange.RateLimited) as caught:
            client.account()
        self.assertEqual(caught.exception.retry_after, 120)
        client, _ = self.make(self.http_error(503))
        with self.assertRaises(exchange.Uncertain):
            client.account()

    def test_network_failure_is_uncertain(self):
        def responder(request):
            raise urllib.error.URLError('down')
        client, _ = self.make(responder)
        with self.assertRaises(exchange.Uncertain):
            client.new_order('BTCUSDT', 'BUY', 'MARKET', 'c', quote_order_qty=5)

    def test_missing_credentials_refused(self):
        with self.assertRaises(ValueError):
            exchange.MexcClient('', '')

    def test_timestamp_follows_mexc_clock_when_computer_clock_is_wrong(self):
        local = [1700000000.0]
        def responder(request):
            if '/api/v3/time' in request.full_url:
                return self.Reply(b'{"serverTime":1700000030000}')
            return self.Reply(b'{}')
        client, seen = self.make(responder, clock=lambda: local[0], synced=False)
        client.account()
        self.assertTrue('/api/v3/time' in seen[0].full_url)
        self.assertEqual(self.stamp(seen[1]), 1700000030000)
        local[0] += 10
        client.account()
        self.assertEqual(len(seen), 3)
        self.assertEqual(self.stamp(seen[2]), 1700000040000)

    def test_offset_is_measured_at_the_midpoint_of_the_round_trip(self):
        ticks = iter([1700000000.0, 1700000002.0])
        def responder(request):
            return self.Reply(b'{"serverTime":1700000001000}')
        client, _ = self.make(responder, clock=lambda: next(ticks), synced=False)
        self.assertTrue(client.sync_time())
        self.assertAlmostEqual(client._offset, 0.0)

    def test_offset_is_refreshed_after_five_minutes(self):
        local = [1700000000.0]
        def responder(request):
            if '/api/v3/time' in request.full_url:
                return self.Reply(b'{"serverTime":%d}' % int(local[0]*1000))
            return self.Reply(b'{}')
        client, seen = self.make(responder, clock=lambda: local[0], synced=False)
        client.account()
        client.account()
        self.assertEqual(len(seen), 3)
        local[0] += exchange.RESYNC_AFTER+1
        client.account()
        self.assertEqual([urllib.parse.urlparse(r.full_url).path.rsplit('/', 1)[1] for r in seen],
                         ['time', 'account', 'account', 'time', 'account'])

    def test_failed_time_lookup_falls_back_to_computer_clock(self):
        def responder(request):
            if '/api/v3/time' in request.full_url:
                raise urllib.error.URLError('down')
            return self.Reply(b'{}')
        client, seen = self.make(responder, synced=False)
        client.account()
        self.assertEqual(self.stamp(seen[1]), 1700000000000)

    def test_timestamp_rejection_resyncs_and_retries_once(self):
        local = [1700000000.0]
        state = dict(offset_ms=0)
        def responder(request):
            if '/api/v3/time' in request.full_url:
                return self.Reply(b'{"serverTime":%d}' % (int(local[0]*1000)+state['offset_ms']))
            if self.stamp(request) != int(local[0]*1000)+state['offset_ms']:
                raise urllib.error.HTTPError(request.full_url, 400, 'x', {}, io.BytesIO(
                    b'{"code":700003,"msg":"Timestamp for this request is outside of the recvWindow"}'))
            return self.Reply(b'{"ok":1}')
        client, seen = self.make(responder, clock=lambda: local[0])
        state['offset_ms'] = 20000
        self.assertEqual(client.account(), {'ok': 1})
        self.assertEqual(len(seen), 3)

    def test_other_rejections_are_not_retried(self):
        client, seen = self.make(self.http_error(400, b'{"code":30004,"msg":"x"}'))
        with self.assertRaises(exchange.ExchangeError):
            client.new_order('BTCUSDT', 'BUY', 'MARKET', 'c', quote_order_qty=5)
        self.assertEqual(len(seen), 1)

    def test_timestamp_rejection_that_persists_raises_after_one_retry(self):
        bad = b'{"code":700003,"msg":"outside of the recvWindow"}'
        def responder(request):
            if '/api/v3/time' in request.full_url:
                return self.Reply(b'{"serverTime":1700000000000}')
            raise urllib.error.HTTPError(request.full_url, 400, 'x', {}, io.BytesIO(bad))
        client, seen = self.make(responder)
        with self.assertRaises(exchange.ExchangeError) as caught:
            client.account()
        self.assertEqual(caught.exception.code, 700003)
        self.assertEqual(len(seen), 3)

    def test_preflight_clock_check_still_uses_raw_server_time(self):
        client, _ = self.make(lambda r: self.Reply(b'{"serverTime":1700000030000}'))
        self.assertEqual(client.server_time(), 1700000030000)


class HelperTests(unittest.TestCase):
    def test_floor_to_step_is_exact(self):
        self.assertEqual(live.floor_to(0.0948, 0.0001), 0.0948)
        self.assertEqual(live.floor_to(0.09489999, 0.0001), 0.0948)
        self.assertEqual(live.floor_to(1.29, 0.1), 1.2)

    def test_not_found_detection(self):
        self.assertTrue(live.not_found(exchange.ExchangeError('Order does not exist', code=-2013)))
        self.assertFalse(live.not_found(exchange.ExchangeError('bad signature', code=700002)))


if __name__ == '__main__':
    unittest.main()


class HardeningTests(Base):
    def test_halt_between_intent_and_submission_cancels_the_buy(self):
        self.buy()
        self.ledger.control('pause', self.now+1)
        self.tick()
        self.assertEqual(self.client.sent, [])
        self.assertEqual(self.state()['cash'], 50.0)

    def test_news_veto_between_intent_and_submission_cancels_the_buy(self):
        self.buy()
        self.ledger.news_block('BTCUSDT', 1, 'exchange notice', self.now+1)
        self.tick()
        self.assertEqual(self.client.sent, [])

    def test_failing_exit_backs_off_instead_of_hammering_the_exchange(self):
        self.buy()
        self.tick()
        self.client.on_new = 'reject'
        self.market.last = 97.0
        self.ledger.mark({'BTCUSDT': 97.0}, self.now+1)
        self.tick()
        sent = len(self.client.sent)
        self.assertEqual(sent, 2)
        self.ledger.mark({'BTCUSDT': 97.0}, self.now+2)
        self.tick()
        self.assertEqual(len(self.client.sent), sent)
        self.ledger.mark({'BTCUSDT': 97.0}, self.now+60)
        self.tick(60)
        self.assertEqual(len(self.client.sent), sent+1)

    def test_repeated_exit_failures_halt_for_human_review(self):
        self.buy()
        self.tick()
        self.client.on_new = 'reject'
        self.market.last = 97.0
        for _ in range(8):
            self.now += 700
            self.ledger.mark({'BTCUSDT': 97.0}, self.now)
            self.executor.cycle(self.now)
        self.assertTrue(self.state()['review_required'])

    def test_second_live_process_is_refused(self):
        other = live.Executor(self.ledger, self.client, self.config, self.market, log=self.logs.append)
        self.assertEqual(other.cycle(self.now+1), 'stopped')
        self.assertTrue(other.stopped)
        self.assertFalse(self.executor.stopped)

    def test_stale_lock_can_be_taken_over(self):
        other = live.Executor(self.ledger, self.client, self.config, self.market, log=self.logs.append)
        self.assertEqual(other.cycle(self.now+31), 'ok')


class ReviewFixTests(Base):
    def test_ledger_file_mode_must_match_the_instance(self):
        self.buy()
        self.tick()
        paper_view = Ledger(self.dir/'live.sqlite', self.config)
        with self.assertRaisesRegex(ValueError, 'live ledger'):
            paper_view.mark({'BTCUSDT': 85.0}, self.now+1)
        self.assertIn('BTCUSDT', self.state()['positions'])
        Ledger(self.dir/'paper.sqlite', self.config).status(NOW)
        with self.assertRaisesRegex(ValueError, 'paper ledger'):
            Ledger(self.dir/'paper.sqlite', self.config, live=True).status(NOW)

    def test_rate_limit_while_reading_trade_history_backs_off(self):
        self.buy()
        original = self.client.my_trades
        self.client.my_trades = lambda s, o: (_ for _ in ()).throw(exchange.RateLimited(120))
        self.assertEqual(self.tick(), 'rate_limited')
        self.assertEqual(len(self.state()['pending']), 1)
        self.assertEqual(self.tick(60), 'rate_limited')
        self.client.my_trades = original
        self.assertEqual(self.tick(61), 'ok')
        self.assertIn('BTCUSDT', self.state()['positions'])
        self.assertEqual(len(self.client.sent), 1)

    def test_rate_limit_while_cancelling_backs_off(self):
        self.buy()
        self.client.on_new = 'lost'
        self.tick()
        self.client.orders['x'] = None
        self.client.query_order = lambda s, c: dict(status='NEW')
        self.client.cancel_order = lambda s, c: (_ for _ in ()).throw(exchange.RateLimited(90))
        self.assertEqual(self.tick(live.WORKING_CANCEL+1), 'rate_limited')
        self.assertEqual(self.tick(30), 'rate_limited')

    def test_old_unsubmitted_buy_is_dropped_not_sent(self):
        self.buy()
        self.assertEqual(self.tick(live.INTENT_MAX_AGE+1), 'ok')
        self.assertEqual(self.client.sent, [])
        state = self.state()
        self.assertEqual((state['cash'], state['pending']), (50.0, {}))

    def test_fresh_buy_intent_still_sent(self):
        self.buy()
        self.tick(live.INTENT_MAX_AGE-1)
        self.assertEqual(len(self.client.sent), 1)

    def test_holding_that_cannot_be_valued_halts_trading(self):
        self.client.balances['ZZZ'] = 5.0
        self.tick()
        self.assertTrue(self.state()['review_required'])
        reasons = [e['reason'] for e in self.state()['events'] if e['kind'] == 'live_halt']
        self.assertIn('unvalued holding ZZZ', reasons[0])

    def test_outage_pricing_an_allowed_coin_does_not_halt(self):
        self.buy()
        self.tick()
        original = self.market.price
        self.market.price = lambda symbol: (_ for _ in ()).throw(RuntimeError('network'))
        self.tick()
        self.market.price = original
        self.assertFalse(self.state()['review_required'])


class PartialSaleRecordTests(Base):
    def test_export_allocates_cost_across_partial_sales(self):
        import csv
        import records
        result = self.buy()
        self.tick()
        position = self.state()['positions']['BTCUSDT']
        cost, qty = position['cost'], position['qty']
        for part, price in ((0.4, 97.0), (0.6, 96.0)):
            self.now += 20
            self.market.last = price
            self.ledger.mark({'BTCUSDT': price}, self.now)
            pending = self.ledger.live_outbox(self.now)
            cid = next(iter(pending))
            sold = qty*part
            self.ledger.live_settle_sell(cid, sold, sold*price, sold*price*.0005, sold*price*.0005, self.now)
        self.assertEqual(self.state()['positions'], {})
        output = self.dir/'records.csv'
        records.export(self.state(), output, flat_rate=1.5)
        with open(output, newline='') as file:
            sells = [r for r in csv.DictReader(file) if r['type'] == 'sell']
        self.assertEqual(len(sells), 2)
        self.assertAlmostEqual(sum(float(r['cost_base_usdt']) for r in sells), cost, places=6)
        self.assertAlmostEqual(float(sells[0]['cost_base_usdt'])/cost, 0.4, places=6)
        self.assertAlmostEqual(float(sells[1]['cost_base_aud']), float(sells[1]['cost_base_usdt'])*1.5)


class SecondReviewTests(Base):
    def fill_times(self, action):
        return [e['timestamp'] for e in self.state()['events'] if e['kind'] == 'fill' and e['action'] == action]

    def test_fill_is_stamped_with_the_exchange_execution_time(self):
        self.buy()
        executed = self.now+5
        self.client.now_ms = int(executed*1000)
        original = self.client.query_order
        self.client.query_order = lambda s, c: (_ for _ in ()).throw(exchange.Uncertain('down'))
        self.tick()
        self.client.query_order = original
        self.tick(100)
        self.assertEqual(self.fill_times('buy'), [executed])

    def test_implausible_exchange_time_falls_back_to_now(self):
        self.buy()
        self.client.now_ms = int((self.now+99999)*1000)
        self.tick()
        self.assertEqual(self.fill_times('buy'), [self.now])

    def test_export_uses_execution_time_for_the_tax_year(self):
        import csv
        import records
        self.buy()
        executed = self.now+5
        self.client.now_ms = int(executed*1000)
        original = self.client.query_order
        self.client.query_order = lambda s, c: (_ for _ in ()).throw(exchange.Uncertain('down'))
        self.tick()
        self.client.query_order = original
        self.tick(100)
        output = self.dir/'r.csv'
        records.export(self.state(), output)
        with open(output, newline='') as file:
            row = next(csv.DictReader(file))
        from datetime import datetime, timezone
        self.assertEqual(row['date_utc'], datetime.fromtimestamp(executed, timezone.utc).isoformat(timespec='seconds'))

    def test_oversized_account_cancels_a_queued_buy(self):
        self.buy()
        self.client.balances['USDT'] = 500
        self.tick()
        self.assertEqual(self.client.sent, [])
        self.assertTrue(self.state()['review_required'])

    def test_foreign_deposit_while_an_order_is_pending_halts(self):
        self.buy()
        self.client.balances['ETH'] = 2.0
        self.tick()
        self.assertEqual(self.client.sent, [])
        self.assertTrue(self.state()['review_required'])

    def test_kill_sells_within_one_executor_cycle_without_a_monitor_pass(self):
        self.buy()
        self.tick()
        sent = len(self.client.sent)
        self.ledger.control('kill', self.now+1)
        self.tick()
        self.assertEqual([s['side'] for s in self.client.sent[sent:]], ['SELL'])
        self.assertEqual(self.state()['positions'], {})

    def test_stop_loss_triggers_from_the_executor_cycle_alone(self):
        self.buy()
        self.tick()
        self.market.last = 97.0
        self.tick()
        self.assertEqual(self.client.sent[-1]['side'], 'SELL')

    def test_price_outage_does_not_break_the_cycle(self):
        self.buy()
        self.tick()
        original = self.market.price
        self.market.price = lambda symbol: (_ for _ in ()).throw(RuntimeError('network'))
        self.assertEqual(self.tick(), 'ok')
        self.market.price = original


class ThirdReviewTests(Base):
    def test_signal_exit_partly_filled_keeps_selling_the_rest(self):
        self.buy()
        self.tick()
        self.client.partial_sell = .5
        self.ledger.signal('exit1', 'BTCUSDT', 'sell', 100.0, 1.0, 'tradingview', 'technical', self.now, self.now, LIQUIDITY)
        self.tick()
        state = self.state()
        self.assertEqual([o['reason'] for o in state['pending'].values()], ['signal'])
        self.assertAlmostEqual(state['positions']['BTCUSDT']['qty'], self.client.balances['BTC'])
        self.client.partial_sell = None
        self.tick()
        self.tick()
        self.assertEqual(self.state()['positions'], {})
        self.assertAlmostEqual(self.client.balances.get('BTC', 0), 0, places=6)

    def test_stop_exit_partly_filled_still_finishes_if_price_rebounds(self):
        self.buy()
        self.tick()
        self.client.partial_sell = .5
        self.market.last = 97.0
        self.tick()
        self.client.partial_sell = None
        self.market.last = 100.0
        self.tick()
        self.tick()
        self.assertEqual(self.state()['positions'], {})

    def test_a_new_exit_never_reuses_a_client_order_id(self):
        self.buy()
        self.tick()
        self.client.partial_sell = .5
        self.market.last = 97.0
        self.tick()
        self.client.partial_sell = None
        self.tick()
        self.tick()
        sells = [s['cid'] for s in self.client.sent if s['side'] == 'SELL']
        self.assertEqual(len(sells), len(set(sells)))
        self.assertGreaterEqual(len(sells), 2)

    def test_one_failing_ticker_does_not_hide_another_positions_stop(self):
        class Ledger:
            marked = None
            def status(self, now):
                return {'positions': {'BTCUSDT': {}, 'ETHUSDT': {}}}
            def mark(self, quotes, now):
                self.marked = quotes
        class Market:
            def price(self, symbol):
                if symbol == 'ETHUSDT':
                    raise RuntimeError('ticker down')
                return 90.0
        ledger = Ledger()
        live.Executor(ledger, None, {'symbols': ['BTCUSDT']}, Market(), log=lambda m: None).mark_positions(1)
        self.assertEqual(ledger.marked, {'BTCUSDT': 90.0})

    def test_market_quotes_skips_symbols_that_fail(self):
        import market
        from unittest.mock import patch
        def price(symbol):
            if symbol == 'ETHUSDT':
                raise RuntimeError('down')
            return 5.0
        with patch('market.price', price):
            self.assertEqual(market.quotes(['BTCUSDT', 'ETHUSDT']), {'BTCUSDT': 5.0})
            self.assertEqual(market.quotes([]), {})

    def test_process_lock_is_exclusive_until_released(self):
        path = self.dir/'live.lock'
        first, second = live.ProcessLock(path), live.ProcessLock(path)
        first.acquire()
        with self.assertRaisesRegex(ValueError, 'already running'):
            second.acquire()
        first.release()
        second.acquire()
        second.release()


class FourthReviewTests(Base):
    def sell_signal(self, identifier='exit1'):
        return self.ledger.signal(identifier, 'BTCUSDT', 'sell', 100.0, 1.0, 'tradingview', 'technical',
                                  self.now, self.now, LIQUIDITY)

    def test_rejected_signal_exit_is_retried_after_the_cooldown(self):
        self.buy()
        self.tick()
        self.client.on_new = 'reject'
        self.sell_signal()
        self.tick()
        self.assertIn('BTCUSDT', self.state()['positions'])
        self.client.on_new = None
        for _ in range(12):
            self.tick(10)
        self.assertEqual(self.state()['positions'], {})
        self.assertEqual([s['side'] for s in self.client.sent], ['BUY', 'SELL', 'SELL'])

    def test_retries_still_respect_the_cooldown(self):
        self.buy()
        self.tick()
        self.client.on_new = 'reject'
        self.sell_signal()
        self.tick()
        sent = len(self.client.sent)
        self.tick(5)
        self.assertEqual(len(self.client.sent), sent)

    def test_one_symbols_data_failure_does_not_block_other_orders(self):
        class Ledger:
            def live_outbox(self, now):
                return {'a': dict(created=1, submitted=None, symbol='AUSDT', side='sell'),
                        'b': dict(created=2, submitted=None, symbol='BUSDT', side='sell')}
            def live_acquire(self, *args):
                pass
        executor = live.Executor(Ledger(), None, {'symbols': []}, None, log=self.logs.append)
        seen = []
        def submit(cid, order, now):
            if cid == 'a':
                raise RuntimeError('ticker down')
            seen.append(cid)
            return False
        executor._submit = submit
        executor.run_outbox(5)
        self.assertEqual(seen, ['b'])
        self.assertEqual(self.logs[-1]['type'], 'RuntimeError')

    def test_rate_limit_still_stops_the_whole_outbox(self):
        class Ledger:
            def live_outbox(self, now):
                return {'a': dict(created=1, submitted=None, symbol='AUSDT', side='sell')}
        executor = live.Executor(Ledger(), None, {'symbols': []}, None, log=self.logs.append)
        def submit(cid, order, now):
            raise exchange.RateLimited(60)
        executor._submit = submit
        with self.assertRaises(exchange.RateLimited):
            executor.run_outbox(5)

    def test_sell_signal_during_unreconciled_buy_is_applied_after_it_settles(self):
        self.buy()
        original = self.client.query_order
        self.client.query_order = lambda s, c: (_ for _ in ()).throw(exchange.Uncertain('down'))
        self.tick()
        result = self.sell_signal()
        self.assertEqual((result['accepted'], result.get('deferred')), (True, True))
        self.client.query_order = original
        for _ in range(4):
            self.tick()
        self.assertEqual(self.state()['positions'], {})
        self.assertEqual([s['side'] for s in self.client.sent], ['BUY', 'SELL'])
        self.assertAlmostEqual(self.client.balances.get('BTC', 0), 0, places=6)

    def test_sell_signal_before_a_buy_is_sent_cancels_the_buy(self):
        self.buy()
        result = self.sell_signal()
        self.assertEqual((result['accepted'], result['cancelled_pending_buy']), (True, True))
        self.tick()
        self.assertEqual(self.client.sent, [])
        state = self.state()
        self.assertEqual((state['cash'], state['pending']), (50.0, {}))

    def test_a_stale_exit_wish_does_not_sell_the_next_purchase(self):
        self.buy('first')
        self.tick()
        self.sell_signal()
        self.tick()
        self.tick()
        self.assertEqual(self.state()['positions'], {})
        self.buy('second')
        self.tick()
        self.tick()
        self.assertIn('BTCUSDT', self.state()['positions'])
        self.assertEqual([s['side'] for s in self.client.sent], ['BUY', 'SELL', 'BUY'])

    def test_a_buy_that_fills_nothing_leaves_no_exit_wish_behind(self):
        self.buy('first')
        self.client.query_order = lambda s, c: (_ for _ in ()).throw(exchange.Uncertain('down'))
        self.tick()
        self.sell_signal()
        self.assertIn('BTCUSDT', self.state().get('exit_wanted', {}))
        cid = next(iter(self.state()['pending']))
        self.ledger.live_release(cid, 'test', self.now)
        self.assertNotIn('BTCUSDT', self.state().get('exit_wanted', {}))


class FifthReviewTests(Base):
    def test_holding_period_uses_the_calendar_anniversary(self):
        import records
        from datetime import datetime, timezone
        def ts(text):
            return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()
        held = records.held_over_twelve_months
        self.assertTrue(held(ts('2023-01-01T01:00:00'), ts('2024-01-02T01:00:00')))
        self.assertFalse(held(ts('2023-01-01T01:00:00'), ts('2024-01-01T01:00:00')))
        self.assertFalse(held(ts('2023-01-01T01:00:00'), ts('2023-06-01T01:00:00')))
        self.assertFalse(held(ts('2024-02-29T01:00:00'), ts('2025-03-01T01:00:00')))
        self.assertTrue(held(ts('2024-02-29T01:00:00'), ts('2025-03-02T01:00:00')))

    def test_export_flags_a_twelve_month_hold(self):
        import csv
        import records
        from datetime import datetime, timezone
        def ts(text):
            return datetime.fromisoformat(text).replace(tzinfo=timezone.utc).timestamp()
        state = dict(mode='paper', events=[
            dict(timestamp=ts('2023-01-01T01:00:00'), kind='fill', action='buy', symbol='BTCUSDT', qty=1, price=100,
                 fee=.1, strategy='t', source='demo'),
            dict(timestamp=ts('2024-01-02T01:00:00'), kind='fill', action='sell', symbol='BTCUSDT', qty=1, price=110,
                 fee=.1, pnl=9.8, reason='signal', strategy='t', source='demo')])
        output = self.dir/'r.csv'
        records.export(state, output, flat_rate=1.5)
        with open(output, newline='') as file:
            sell = [r for r in csv.DictReader(file) if r['type'] == 'sell'][0]
        self.assertEqual(sell['held_over_12_months'], 'yes')

    def zero_fill_sells(self):
        def refuse(symbol, side, order_type, cid, quantity=None, quote_order_qty=None):
            self.client.sent.append(dict(symbol=symbol, side=side, type=order_type, cid=cid, quantity=quantity,
                                         quote=quote_order_qty))
            self.client.orders[cid] = dict(orderId='z'+cid[:6], status='CANCELED', executedQty='0',
                                           cumulativeQuoteQty='0')
            return {}
        self.client.new_order = refuse

    def test_zero_fill_cancellations_back_off_instead_of_hammering(self):
        self.buy()
        self.tick()
        self.zero_fill_sells()
        self.ledger.signal('x1', 'BTCUSDT', 'sell', 100.0, 1.0, 'tradingview', 'technical', self.now, self.now, LIQUIDITY)
        for _ in range(6):
            self.tick()
        sells = [s for s in self.client.sent if s['side'] == 'SELL']
        self.assertLessEqual(len(sells), 2)
        self.assertGreaterEqual(self.state()['exit_fails']['BTCUSDT'], 1)

    def test_repeated_zero_fill_cancellations_halt_for_review(self):
        self.buy()
        self.tick()
        self.zero_fill_sells()
        self.ledger.signal('x1', 'BTCUSDT', 'sell', 100.0, 1.0, 'tradingview', 'technical', self.now, self.now, LIQUIDITY)
        for _ in range(12):
            self.now += 700
            self.executor.cycle(self.now)
        self.assertTrue(self.state()['review_required'])
        self.assertIn('BTCUSDT', self.state()['positions'])

    def test_base_coin_commission_is_visible_without_double_counting(self):
        self.client.fee_asset = 'BTC'
        self.client.fee_rate = .0005
        self.buy()
        self.tick()
        state = self.state()
        fill = [e for e in state['events'] if e['kind'] == 'fill' and e['action'] == 'buy'][0]
        quote = self.client.sent[0]['quote']
        self.assertAlmostEqual(fill['price'], 100.0, places=6)
        self.assertAlmostEqual(fill['fee'], fill['qty']*100.0*.0005/(1-.0005), places=6)
        self.assertAlmostEqual(state['positions']['BTCUSDT']['cost'], quote, places=6)
        self.assertAlmostEqual(fill['qty']*fill['price']+fill['fee'], quote, places=4)


class DoctorTests(unittest.TestCase):
    def trial(self, days=40, trades=60, pnl=1.0, halted=False):
        events = [dict(timestamp=NOW-days*86400, kind='fill', action='buy', symbol='BTCUSDT')]
        events += [dict(timestamp=NOW-86400, kind='fill', action='sell', symbol='BTCUSDT', pnl=pnl/trades)
                   for _ in range(trades)]
        return dict(events=events, review_required=halted)

    def config(self, eligible=True, key=True):
        config = copy.deepcopy(CONFIG)
        config['live'] = dict(mexc_eligibility_confirmed=eligible, trade_only_key_confirmed=key)
        return config

    def test_fresh_install_lists_what_is_missing_in_order(self):
        report = live.doctor(self.config(False, False), self.trial(days=0, trades=0, pnl=0), NOW, False, False, (3, 13))
        names = [s['name'] for s in report['steps'] if not s['ok']]
        self.assertIn('Webhook secret created', names)
        self.assertIn('Paper trading for 30 days', names)
        self.assertFalse(report['ready_for_preflight'])
        self.assertEqual(report['next_step'], 'Type: python bot.py setup')

    def test_everything_done_points_to_the_preflight(self):
        report = live.doctor(self.config(), self.trial(), NOW, True, True, (3, 13))
        self.assertTrue(report['ready_for_preflight'])
        self.assertEqual(report['next_step'], 'Type: python bot.py live-preflight')

    def test_each_missing_item_blocks_readiness(self):
        cases = [dict(trial=self.trial(days=5)), dict(trial=self.trial(trades=3)), dict(trial=self.trial(pnl=-1)),
                 dict(trial=self.trial(halted=True)), dict(config=self.config(eligible=False)),
                 dict(config=self.config(key=False)), dict(secret=False), dict(keys=False), dict(python=(3, 9))]
        for case in cases:
            report = live.doctor(case.get('config', self.config()), case.get('trial', self.trial()), NOW,
                                 case.get('secret', True), case.get('keys', True), case.get('python', (3, 13)))
            self.assertFalse(report['ready_for_preflight'], case)

    def test_report_contains_no_secret_values(self):
        with patch.dict('os.environ', {'MEXC_API_KEY': 'KEYVALUE123', 'MEXC_API_SECRET': 'SECRETVALUE456'}):
            text = json.dumps(live.doctor(self.config(), self.trial(), NOW, True, True, (3, 13)))
        self.assertNotIn('KEYVALUE123', text)
        self.assertNotIn('SECRETVALUE456', text)


if __name__ == '__main__':
    unittest.main()

