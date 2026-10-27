"""Stage 1: paper-only CLI and authenticated webhook, using Python's standard library."""
import argparse
import csv
import hmac
import json
import math
import os
import secrets
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import copytrade
import exchange
import live
import market
import records
from risk import Ledger, finite
from strategies import evaluate, indicators

ROOT = Path(__file__).resolve().parent
LIVE_COMMANDS = {'status', 'pause', 'resume', 'kill', 'review-rearm', 'news-list', 'news-block', 'news-clear',
                 'copy-report', 'export-records'}


def load_config(path):
    config = json.loads(Path(path).read_text())
    if config['mode'] != 'paper':
        raise ValueError('config.json must say mode paper (PAPER ONLY). Real trading starts only with live-start after live-preflight passes.')
    if config['option'] not in ('A', 'B', 'C'):
        raise ValueError('Option must be A, B or C.')
    if config['news'] is not (config['option'] == 'C'):
        raise ValueError('News veto is part of option C: news must be true for C and false for A or B.')
    if type(config['copy_trading']) is not bool:
        raise ValueError('copy_trading must be true or false.')
    confirmations = config.get('live', {})
    if not isinstance(confirmations, dict) or set(confirmations) - {'mexc_eligibility_confirmed', 'trade_only_key_confirmed'} \
            or any(type(v) is not bool for v in confirmations.values()):
        raise ValueError('live must contain only true/false values for mexc_eligibility_confirmed and trade_only_key_confirmed.')
    if config['tradovate'] or config['portal_strategies']:
        raise ValueError('Tradovate and portal strategies are not implemented; keep them disabled.')
    for name in ('total_cap','position_cap','floor','risk_per_trade','daily_loss','fee','slippage','min_paper_order'):
        if not finite(config[name]) or config[name] <= 0:
            raise ValueError(f'{name} must be a positive finite number.')
    if not 0 < config['daily_loss'] <= .10 or not 0 < config['risk_per_trade'] <= .01:
        raise ValueError('Daily loss may not exceed 10%; per-trade risk may not exceed 1%.')
    if config['fee'] >= .1 or config['slippage'] >= .1:
        raise ValueError('Invalid cost estimates.')
    if type(config['max_positions']) is not int or not 1 <= config['max_positions'] <= 5:
        raise ValueError('max_positions must be an integer from 1 to 5.')
    if not config['symbols'] or any(not s.isalnum() or not s.endswith('USDT') for s in config['symbols']):
        raise ValueError('Allowlist must contain explicit USDT spot symbols.')
    return config


def load_secrets():
    path = ROOT/'.env'
    if path.exists():
        for line in path.read_text().splitlines():
            if '=' in line and not line.startswith('#'):
                key, value = line.split('=', 1)
                if key == 'WEBHOOK_SECRET':
                    os.environ.setdefault(key, value.strip())
    token = os.environ.get('WEBHOOK_SECRET', '')
    if len(token) < 32:
        raise ValueError('Run setup first or set WEBHOOK_SECRET to a random value of at least 32 characters.')
    return token


def mode_of(ledger):
    return 'live' if ledger.live else 'paper'


def process_alert(payload, token, ledger, config, now):
    allowed = {'secret','id','symbol','action','strategy','timeframe','sent_at','candle_time'}
    if not isinstance(payload, dict) or set(payload) != allowed:
        raise ValueError('Webhook must contain exactly the documented fields.')
    if not isinstance(payload['secret'], str) or not hmac.compare_digest(payload['secret'], token):
        raise PermissionError('Authentication failed.')
    if (not isinstance(payload['id'], str) or not 1 <= len(payload['id']) <= 160
        or payload['symbol'] not in config['symbols'] or payload['action'] not in ('buy','sell')
        or payload['strategy'] != 'technical' or payload['timeframe'] != '1h'):
        raise ValueError('Unsupported alert ID, symbol, action, strategy, or timeframe.')
    if not finite(payload['sent_at']) or not 0 <= now-payload['sent_at'] <= 120:
        raise ValueError('Alert is expired or future-dated; timestamp must be Unix seconds.')
    if not finite(payload['candle_time']):
        raise ValueError('Invalid candle timestamp.')
    bars = market.candles(payload['symbol'])
    if not bars or abs(bars[-1]['time']-payload['candle_time']) > 1:
        raise ValueError('Alert does not reference the latest closed MEXC one-hour candle.')
    action, i = evaluate(bars, 'technical')
    if action != payload['action']:
        return {'accepted':False, 'reason':'technical_confirmation_failed', 'mode':mode_of(ledger)}
    quote = market.price(payload['symbol'])
    ledger.mark({payload['symbol']:quote}, now)
    liquid = market.liquidity(payload['symbol']) if action == 'buy' else None
    return ledger.signal(payload['id'], payload['symbol'], action, quote, i['atr'],
                         'tradingview', 'technical', now, payload['sent_at'], liquid)


def auto_status_veto(ledger, config, market_data, now):
    blocked = []
    if not config['news']:
        return blocked
    for symbol in config['symbols']:
        try:
            status = market_data.trading_status(symbol)
        except Exception:
            continue
        if not status['ok'] and ledger.auto_block(symbol, f"exchange status {status['status']}: not online", now):
            blocked.append(symbol)
    return blocked


def make_handler(ledger, token):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Never log payloads, tokens, or query strings.
        def reply(self, code, data):
            raw = json.dumps(data).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)
        def do_GET(self):
            if self.path == '/health':
                self.reply(200, {'mode':mode_of(ledger),'live_enabled':ledger.live})
            else:
                self.reply(404, {'error':'No dashboard or remote control endpoint exists.'})
        def do_POST(self):
            if self.path != '/webhook':
                return self.reply(404, {'error':'Unknown endpoint.'})
            try:
                self.connection.settimeout(2)
                size = int(self.headers.get('Content-Length', '0'))
                if not 0 < size <= 4096:
                    raise ValueError('Payload must be between 1 and 4096 bytes.')
                payload = json.loads(self.rfile.read(size), parse_constant=lambda _:None)
                # Persist before acknowledging; processing happens outside TradingView's short timeout.
                # A queued alert is not a simulated fill.
                if not isinstance(payload, dict) or not isinstance(payload.get('secret'), str):
                    raise ValueError('Missing authentication.')
                if not hmac.compare_digest(payload['secret'], token):
                    raise PermissionError('Authentication failed.')
                payload = dict(payload)
                payload.pop('secret')
                def enqueue(state):
                    queued = state.setdefault('queue', [])
                    if len(queued) >= 100:
                        raise ValueError('Queue full; no alert accepted.')
                    queued.append(payload)
                ledger.transact(time.time(), enqueue)
                self.reply(202, {'queued':True,'mode':mode_of(ledger),'filled':False})
            except PermissionError:
                self.reply(401, {'error':'Authentication failed.'})
            except (ValueError, TypeError, TimeoutError, json.JSONDecodeError):
                self.reply(400, {'error':'Invalid request; no trade performed.'})
    return Handler


def serve(ledger, config, port, executor=None):
    token = load_secrets()
    stop = threading.Event()
    def worker():
        while not stop.is_set():
            def dequeue(state):
                queue = state.setdefault('queue', [])
                return queue.pop(0) if queue else None
            payload = ledger.transact(time.time(), dequeue)
            if payload:
                try:
                    payload['secret'] = token
                    result = process_alert(payload, token, ledger, config, time.time())
                    print(json.dumps(result), flush=True)
                except Exception as exc:
                    print(json.dumps({'alert_rejected':type(exc).__name__, 'mode':mode_of(ledger)}), flush=True)
            else:
                stop.wait(1)

    def trader():
        while not stop.is_set():
            try:
                executor.cycle(time.time())
            except Exception as exc:
                print(json.dumps({'live_cycle_error': type(exc).__name__}), flush=True)
            if executor.stopped:
                stop.set()
                server.shutdown()
            stop.wait(5)

    def monitor():
        last_status_check = 0.0
        while not stop.is_set():
            try:
                if time.time()-last_status_check >= 300:
                    last_status_check = time.time()
                    for symbol in auto_status_veto(ledger, config, market, time.time()):
                        print(json.dumps({'auto_veto': symbol, 'reason': 'exchange status not online'}), flush=True)
                state = ledger.status(time.time())
                quotes = market.quotes(state['positions'])
                if quotes:
                    ledger.mark(quotes, time.time())
                if config['option'] in ('B', 'C'):
                    for candidate in market.scan(config['symbols']):
                        if candidate['action'] == 'hold':
                            continue
                        symbol = candidate['symbol']
                        ledger.signal(f"scanner:{symbol}:{candidate['candle_time']}", symbol,
                            candidate['action'], market.price(symbol), candidate['atr'], 'scanner',
                            'breakout', time.time(), liquidity=candidate['liquidity'])
            except Exception as exc:
                # Do not clear stops or safety latches when market data is unavailable.
                print(json.dumps({'market_data_error':type(exc).__name__, 'entries_unavailable':True}), flush=True)
            stop.wait(15 if config['option'] == 'A' else 60)

    server = ThreadingHTTPServer(('127.0.0.1', port), make_handler(ledger, token))
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=monitor, daemon=True).start()
    if executor:
        threading.Thread(target=trader, daemon=True).start()
        print(f'LIVE TRADING WITH REAL MONEY. Webhook: http://127.0.0.1:{port}/webhook. Kill switch: python bot.py --live kill')
    else:
        print(f'PAPER ONLY. Webhook: http://127.0.0.1:{port}/webhook. No real orders or exchange keys.')
    if config['news']:
        print('NEWS VETO ON: it blocks only the entries you record with news-block. No news provider is connected.')
    if config['copy_trading']:
        print('COPY TRADING SLOT ON: no provider is connected; nothing is copied automatically.')
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()


def demo(config):
    with tempfile.TemporaryDirectory() as directory:
        ledger = Ledger(Path(directory)/'demo.sqlite', config)
        now = time.time()
        result = ledger.signal('demo-entry', 'BTCUSDT', 'buy', 100, 1,
                               'demo', 'technical', now)
        ledger.mark({'BTCUSDT':105}, now+1)
        state = ledger.status(now+2)
        return dict(synthetic_demo=True, not_market_evidence=True, independent_temporary_ledger=True,
                    entry=result, equity=state['equity'], positions=state['positions'])


def backtest(path, strategy, config):
    with open(path, newline='') as file:
        bars = [{k:float(v) for k,v in r.items()} for r in csv.DictReader(file)]
    required = {'time','open','high','low','close','volume'}
    if len(bars) < 62:
        raise ValueError('At least 62 historical bars required.')
    for index, bar in enumerate(bars):
        if set(bar) != required or not all(math.isfinite(v) for v in bar.values()):
            raise ValueError('CSV needs finite time, open, high, low, close, volume columns.')
        if bar['volume'] < 0 or min(bar['open'], bar['high'], bar['low'], bar['close']) <= 0:
            raise ValueError('Invalid candle values.')
        if not bar['low'] <= min(bar['open'],bar['close']) <= max(bar['open'],bar['close']) <= bar['high']:
            raise ValueError('Invalid candle price range.')
        if index and bar['time']-bars[index-1]['time'] != 3600:
            raise ValueError('CSV must be chronological, gap-free one-hour candles.')
    symbol = config['symbols'][0]
    with tempfile.TemporaryDirectory() as directory:
        ledger = Ledger(Path(directory)/'backtest.sqlite', config)
        peak, drawdown = config['paper_cash'], 0
        for index in range(60, len(bars)):
            bar = bars[index]
            now = bar['time']
            ledger.mark({symbol:bar['open']}, now)
            action, i = evaluate(bars[max(0,index-200):index], strategy)
            if action != 'hold':
                ledger.signal(f'backtest:{index}', symbol, action, bar['open'], i['atr'],
                              'backtest', strategy, now)
            # Conservative ordering: low/stop before high/take-profit when both occur.
            ledger.mark({symbol:bar['low']}, now+1)
            ledger.mark({symbol:bar['high']}, now+2)
            equity = ledger.mark({symbol:bar['close']}, now+3599)
            peak = max(peak, equity)
            drawdown = max(drawdown, (peak-equity)/peak)
        state = ledger.status(bars[-1]['time']+3599)
        fills = [e for e in state['events'] if e['kind']=='fill' and e['action']=='sell']
        return dict(strategy=strategy, equity=state['equity'], return_pct=100*(state['equity']/config['paper_cash']-1),
                    max_drawdown_pct=100*drawdown, closed_trades=len(fills),
                    fees=sum(e.get('fee',0) for e in state['events']), safety_pause=state['review_required'],
                    assumptions='USDT~USD; synthetic liquidity; no exchange filters or intrabar ticks. Not live qualification.')


def confirm_live(read=input):
    print('REAL MONEY WARNING. This starts automatic trading on your MEXC account, up to 50 USD total and 10 USD per position.')
    print('You can lose all of it. Nothing here is a promise of profit. Local stop-losses only work while this program runs.')
    print(f'To continue, type exactly: {live.PHRASE}')
    try:
        typed = read('> ')
    except EOFError:
        typed = ''
    if typed.strip() != live.PHRASE:
        raise ValueError('Confirmation phrase not typed exactly; live trading was not started.')


def live_checks(config, args, now):
    keys = bool(os.environ.get('MEXC_API_KEY')) and bool(os.environ.get('MEXC_API_SECRET'))
    client = exchange.MexcClient.from_env() if keys else None
    paper_state = Ledger(args.paper_state, config).status(now)
    checks, ready = live.preflight(config, client, market, paper_state, now, keys)
    return client, checks, ready


def main():
    parser = argparse.ArgumentParser(description='MEXC bot. Paper by default; live trading needs live-start.')
    parser.add_argument('--config', default=str(ROOT/'config.json'))
    parser.add_argument('--state', default=None, help='ledger file; defaults to paper.sqlite, or live.sqlite with --live')
    parser.add_argument('--live', action='store_true', help='use the live ledger for status and safety controls')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('init','status','pause','resume','kill','demo','setup','scan','news-list','copy-status','copy-report',
                 'live-preflight', 'doctor'):
        sub.add_parser(name)
    block = sub.add_parser('news-block')
    block.add_argument('--symbol', required=True, help='an allowed symbol such as BTCUSDT, or * for all')
    block.add_argument('--hours', type=float, required=True)
    block.add_argument('--reason', required=True)
    clear = sub.add_parser('news-clear')
    clear.add_argument('--symbol', required=True)
    clear.add_argument('--note', required=True)
    copy_signal = sub.add_parser('copy-signal')
    copy_signal.add_argument('--provider', choices=copytrade.PROVIDERS, required=True)
    copy_signal.add_argument('--id', required=True)
    copy_signal.add_argument('--symbol', required=True)
    copy_signal.add_argument('--action', choices=('buy','sell'), required=True)
    copy_signal.add_argument('--seconds-ago', type=float, required=True)
    export = sub.add_parser('export-records')
    export.add_argument('--output', required=True)
    export.add_argument('--rates', help='CSV with columns date, aud_per_usdt')
    export.add_argument('--flat-rate', type=float, help='one AUD per USDT rate for every row')
    review = sub.add_parser('review-rearm')
    review.add_argument('--note', required=True)
    start = sub.add_parser('start')
    start.add_argument('--port', type=int, default=8787)
    live_start = sub.add_parser('live-start')
    live_start.add_argument('--port', type=int, default=8787)
    test = sub.add_parser('backtest')
    test.add_argument('--csv', required=True)
    test.add_argument('--strategy', choices=('technical','breakout'), required=True)
    download = sub.add_parser('download-history')
    download.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        args.paper_state = args.state if args.state and not args.live else str(ROOT/'paper.sqlite')
        args.state = args.state or str(ROOT/('live.sqlite' if args.live else 'paper.sqlite'))
        if args.live and args.command not in LIVE_COMMANDS:
            raise ValueError('--live only applies to status, safety controls, news veto and records; use live-start to trade.')
        ledger = Ledger(args.state, config, live=args.live)
        if args.command in ('init','status'):
            result = ledger.status(time.time())
            if args.command == 'init':
                result = {'mode':'paper','equity':result['equity'],'state_file':args.state,'live_enabled':False}
        elif args.command in ('pause','resume','kill','review-rearm'):
            result = ledger.control(args.command, time.time(), getattr(args,'note',''))
        elif args.command == 'news-block':
            result = ledger.news_block(args.symbol, args.hours, args.reason, time.time())
        elif args.command == 'news-clear':
            result = ledger.news_clear(args.symbol, args.note, time.time())
        elif args.command == 'news-list':
            result = ledger.news_list(time.time())
        elif args.command == 'copy-status':
            result = copytrade.status(config)
        elif args.command == 'copy-signal':
            if not finite(args.seconds_ago) or args.seconds_ago < 0:
                raise ValueError('seconds-ago must be zero or more.')
            now = time.time()
            event = dict(provider=args.provider, id=args.id, symbol=args.symbol,
                         action=args.action, source_time=now-args.seconds_ago)
            result = copytrade.process_event(event, ledger, config, now, market)
        elif args.command == 'copy-report':
            result = copytrade.report(ledger.status(time.time()))
        elif args.command == 'export-records':
            result = records.export(ledger.status(time.time()), args.output, args.rates, args.flat_rate)
        elif args.command == 'demo':
            result = demo(config)
        elif args.command == 'setup':
            path = ROOT/'.env'
            if path.exists():
                raise ValueError('.env already exists; setup will not overwrite your secret.')
            descriptor = os.open(path, os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600)
            with os.fdopen(descriptor,'w') as file:
                file.write('WEBHOOK_SECRET='+secrets.token_urlsafe(32)+'\n')
            result = {'created':str(path),'next':'Copy only WEBHOOK_SECRET from this private file into your own TradingView alert. Never paste exchange keys into alerts.'}
        elif args.command == 'start':
            serve(ledger, config, args.port)
            return
        elif args.command == 'doctor':
            try:
                load_secrets()
                has_secret = True
            except ValueError:
                has_secret = False
            keys = bool(os.environ.get('MEXC_API_KEY')) and bool(os.environ.get('MEXC_API_SECRET'))
            paper_state = Ledger(args.paper_state, config).status(time.time())
            result = live.doctor(config, paper_state, time.time(), has_secret, keys, sys.version_info[:2])
        elif args.command == 'live-preflight':
            _, checks, ready = live_checks(config, args, time.time())
            result = dict(ready=ready, checks=checks, nothing_was_traded=True)
        elif args.command == 'live-start':
            load_secrets()
            process_lock = live.ProcessLock(ROOT/'live.lock')
            process_lock.acquire()
            now = time.time()
            client, checks, ready = live_checks(config, args, now)
            if not ready:
                print(json.dumps(dict(ready=False, checks=checks), indent=2))
                raise ValueError('Live preflight failed; live trading was not started.')
            confirm_live()
            live_ledger = Ledger(str(ROOT/'live.sqlite'), config, live=True)
            executor = live.Executor(live_ledger, client, config, market, log=lambda m: print(json.dumps(m), flush=True))
            executor.cycle(time.time())
            serve(live_ledger, config, args.port, executor)
            return
        elif args.command == 'scan':
            result = {'candidates':market.scan(config['symbols']),'orders_placed':False}
        elif args.command == 'backtest':
            result = backtest(args.csv, args.strategy, config)
        elif args.command == 'download-history':
            bars = market.candles(config['symbols'][0], limit=1000)
            with open(args.output,'x',newline='') as file:
                writer = csv.DictWriter(file, fieldnames=['time','open','high','low','close','volume'])
                writer.writeheader()
                writer.writerows(bars)
            result = {'saved':args.output,'closed_bars':len(bars),'note':'Short sample; not enough for qualification.'}
        print(json.dumps(result, indent=2))
    except (ValueError, RuntimeError, OSError, KeyError) as exc:
        parser.exit(1, f'Error: {exc}\n')


if __name__ == '__main__':
    main()
