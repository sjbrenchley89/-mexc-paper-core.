"""Copy-signal slot. Both provider adapters are disconnected; nothing here reads a provider account."""
from risk import finite
from strategies import indicators

PROVIDERS = ('fomo', 'astral')
FIELDS = {'provider', 'id', 'symbol', 'action', 'source_time'}
REASON = ('No official, permitted feed has been confirmed. Never share account passwords or '
          'scrape; confirm the provider terms allow unattended mirroring first.')


class DisconnectedAdapter:
    connected = False

    def __init__(self, provider):
        self.provider = provider

    def poll(self):
        return []

    def describe(self):
        return dict(provider=self.provider, connected=False, reason=REASON)


def adapters():
    return {name: DisconnectedAdapter(name) for name in PROVIDERS}


def status(config):
    return dict(copy_trading_enabled=bool(config.get('copy_trading')),
                automatic_copying=False,
                providers=[a.describe() for a in adapters().values()],
                manual_relay='copy-signal passes one signal you observed through the same risk checks.')


def process_event(event, ledger, config, now, market):
    if not config.get('copy_trading'):
        raise ValueError('Copy trading is off in config.json.')
    if not isinstance(event, dict) or set(event) != FIELDS:
        raise ValueError('Copy event must contain exactly the documented fields.')
    if event['provider'] not in PROVIDERS:
        raise ValueError('Unknown provider.')
    if not isinstance(event['id'], str) or not 1 <= len(event['id']) <= 120:
        raise ValueError('Event ID must be 1 to 120 characters.')
    if event['symbol'] not in config['symbols'] or event['action'] not in ('buy', 'sell'):
        raise ValueError('Unsupported symbol or action.')
    if not finite(event['source_time']):
        raise ValueError('Invalid source time.')
    symbol = event['symbol']
    atr = indicators(market.candles(symbol))['atr']
    quote = market.price(symbol)
    ledger.mark({symbol: quote}, now)
    liquid = market.liquidity(symbol) if event['action'] == 'buy' else None
    return ledger.signal(f"copy:{event['provider']}:{event['id']}", symbol, event['action'], quote, atr,
                         'copy', f"copy:{event['provider']}", now, event['source_time'], liquid)


def report(state):
    rows = {name: dict(entries=0, closed_trades=0, net_pnl_usdt=0.0, rejections={}, _latency=[])
            for name in PROVIDERS}
    for e in state['events']:
        if e['kind'] == 'rejection' and e.get('source') == 'copy':
            provider = e['id'].split(':')[1]
            row = rows.get(provider)
            if row:
                row['rejections'][e['reason']] = row['rejections'].get(e['reason'], 0)+1
        elif e['kind'] == 'fill' and e.get('source') == 'copy':
            row = rows.get(e['strategy'].split(':')[1])
            if not row:
                continue
            if e['action'] == 'buy':
                row['entries'] += 1
                if e.get('latency') is not None:
                    row['_latency'].append(e['latency'])
            else:
                row['closed_trades'] += 1
                row['net_pnl_usdt'] += e['pnl']
    for row in rows.values():
        latency = row.pop('_latency')
        row['avg_latency_seconds'] = sum(latency)/len(latency) if latency else None
    return dict(by_provider=rows,
                note='Paper results only. Compare with each provider\'s own reported results yourself; '
                     'late or rejected signals are counted, not hidden.')
