"""Stage 1: paper-only CLI and authenticated webhook, using Python's standard library."""
import argparse
import csv
import hmac
import json
import math
import os
import secrets
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import market
from risk import Ledger, finite
from strategies import evaluate, indicators

ROOT = Path(__file__).resolve().parent


def load_config(path):
    config = json.loads(Path(path).read_text())
    if config['mode'] != 'paper':
        raise ValueError('Stage 1 is PAPER ONLY. Live execution is not implemented or enabled.')
    if config['option'] not in ('A', 'B'):
        raise ValueError('Option C needs an identified, permitted news provider and is deferred.')
    if any(config.get(k) for k in ('copy_trading', 'tradovate', 'portal_strategies', 'news')):
        raise ValueError('Optional account connections are deferred; keep them disabled.')
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
        return {'accepted':False, 'reason':'technical_confirmation_failed', 'mode':'paper'}
    quote = market.price(payload['symbol'])
    ledger.mark({payload['symbol']:quote}, now)
    liquid = market.liquidity(payload['symbol']) if action == 'buy' else None
    return ledger.signal(payload['id'], payload['symbol'], action, quote, i['atr'],
                         'tradingview', 'technical', now, payload['sent_at'], liquid)


def serve(ledger, config, port):
    token = load_secrets()
    stop = threading.Event()
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
                self.reply(200, {'mode':'paper','live_enabled':False})
            else:
                self.reply(404, {'error':'No dashboard or remote control endpoint in stage 1.'})
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
                self.reply(202, {'queued':True,'mode':'paper','filled':False})
            except PermissionError:
                self.reply(401, {'error':'Authentication failed.'})
            except (ValueError, TypeError, TimeoutError, json.JSONDecodeError):
                self.reply(400, {'error':'Invalid request; no trade performed.'})

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
                    print(json.dumps({'alert_rejected':type(exc).__name__, 'mode':'paper'}), flush=True)
            else:
                stop.wait(1)

    def monitor():
        while not stop.is_set():
            try:
                state = ledger.status(time.time())
                quotes = {symbol:market.price(symbol) for symbol in state['positions']}
                ledger.mark(quotes, time.time())
                if config['option'] == 'B':
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

    server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=monitor, daemon=True).start()
    print(f'PAPER ONLY. Webhook: http://127.0.0.1:{port}/webhook. No real orders or exchange keys.')
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


def main():
    parser = argparse.ArgumentParser(description='MEXC paper core. Live execution is unavailable.')
    parser.add_argument('--config', default=str(ROOT/'config.json'))
    parser.add_argument('--state', default=str(ROOT/'paper.sqlite'))
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('init','status','pause','resume','kill','demo','setup','scan'):
        sub.add_parser(name)
    review = sub.add_parser('review-rearm')
    review.add_argument('--note', required=True)
    start = sub.add_parser('start')
    start.add_argument('--port', type=int, default=8787)
    test = sub.add_parser('backtest')
    test.add_argument('--csv', required=True)
    test.add_argument('--strategy', choices=('technical','breakout'), required=True)
    download = sub.add_parser('download-history')
    download.add_argument('--output', required=True)
    args = parser.parse_args()
    try:
        config = load_config(args.config)
        ledger = Ledger(args.state, config)
        if args.command in ('init','status'):
            result = ledger.status(time.time())
            if args.command == 'init':
                result = {'mode':'paper','equity':result['equity'],'state_file':args.state,'live_enabled':False}
        elif args.command in ('pause','resume','kill','review-rearm'):
            result = ledger.control(args.command, time.time(), getattr(args,'note',''))
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
