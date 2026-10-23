"""Trade-record CSV with AUD columns for Australian record-keeping. Not tax advice."""
import csv
from datetime import date, datetime, timedelta, timezone

from risk import finite

COLUMNS = ['date_utc', 'tax_year', 'mode', 'type', 'symbol', 'quantity', 'price_usdt', 'gross_usdt',
           'fee_usdt', 'cost_base_usdt', 'proceeds_usdt', 'gain_usdt', 'strategy', 'source', 'reason',
           'days_held', 'held_over_12_months', 'aud_per_usdt', 'gross_aud', 'fee_aud',
           'cost_base_aud', 'proceeds_aud', 'gain_aud']
NOTE = ('Paper trades are simulated and are not tax records. For real trades, keep AUD values, '
        'your AUD to USDT purchases, transfers and exchange statements as well. Confirm treatment '
        'with a registered tax agent.')


def utc(timestamp):
    return datetime.fromtimestamp(timestamp, timezone.utc)


def tax_year(timestamp):
    # 30 June is always AEST (UTC+10), so a fixed offset places the 1 July boundary exactly.
    local = utc(timestamp)+timedelta(hours=10)
    start = local.year if local.month >= 7 else local.year-1
    return f'{start}-{(start+1)%100:02d}'


def load_rates(path):
    rates = {}
    with open(path, newline='') as file:
        reader = csv.DictReader(file)
        if reader.fieldnames is None or set(reader.fieldnames) != {'date', 'aud_per_usdt'}:
            raise ValueError('Rates file needs exactly two columns: date, aud_per_usdt.')
        for row in reader:
            try:
                day, rate = date.fromisoformat(row['date']).isoformat(), float(row['aud_per_usdt'])
            except (TypeError, ValueError):
                raise ValueError('Rates file has an invalid date or rate.') from None
            if not finite(rate) or rate <= 0 or day in rates:
                raise ValueError('Rates must be positive numbers with one row per date.')
            rates[day] = rate
    return rates


def export(state, output, rates_path=None, flat_rate=None):
    if rates_path and flat_rate is not None:
        raise ValueError('Use either a rates file or a flat rate, not both.')
    rates = load_rates(rates_path) if rates_path else None
    if flat_rate is not None and (not finite(flat_rate) or flat_rate <= 0):
        raise ValueError('Flat rate must be a positive number.')
    has_aud = rates is not None or flat_rate is not None
    fills = [e for e in state['events'] if e['kind'] == 'fill']
    if rates is not None:
        missing = sorted({utc(e['timestamp']).date().isoformat() for e in fills}-set(rates))
        if missing:
            raise ValueError('Rates file has no rate for: '+', '.join(missing))

    def rate_for(timestamp):
        if flat_rate is not None:
            return flat_rate
        return rates[utc(timestamp).date().isoformat()] if rates is not None else None

    def aud(value, rate):
        return '' if rate is None or value == '' else value*rate

    open_buys, rows = {}, []
    for e in fills:
        rate = rate_for(e['timestamp'])
        gross = e['qty']*e['price']
        row = dict.fromkeys(COLUMNS, '')
        row.update(date_utc=utc(e['timestamp']).isoformat(timespec='seconds'), tax_year=tax_year(e['timestamp']),
                   mode=state['mode'], type=e['action'], symbol=e['symbol'], quantity=e['qty'],
                   price_usdt=e['price'], gross_usdt=gross, fee_usdt=e['fee'], strategy=e['strategy'],
                   source=e['source'], aud_per_usdt='' if rate is None else rate)
        if e['action'] == 'buy':
            open_buys[e['symbol']] = dict(time=e['timestamp'], cost=gross+e['fee'], rate=rate)
        else:
            buy = open_buys.pop(e['symbol'], None)
            if buy is None:
                raise ValueError('Ledger has a sale with no matching purchase; export stopped.')
            proceeds = gross-e['fee']
            days = (e['timestamp']-buy['time'])/86400
            row.update(reason=e['reason'], cost_base_usdt=buy['cost'], proceeds_usdt=proceeds,
                       gain_usdt=proceeds-buy['cost'], days_held=round(days, 4),
                       held_over_12_months='yes' if days > 366 else 'no',
                       cost_base_aud=aud(buy['cost'], buy['rate']), proceeds_aud=aud(proceeds, rate))
            if rate is not None:
                row['gain_aud'] = row['proceeds_aud']-row['cost_base_aud']
        row.update(gross_aud=aud(gross, rate), fee_aud=aud(e['fee'], rate))
        rows.append(row)

    with open(output, 'x', newline='') as file:
        writer = csv.DictWriter(file, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    totals = {}
    for row in rows:
        if row['type'] == 'sell':
            year = totals.setdefault(row['tax_year'], dict(gain_usdt=0.0, gain_aud=0.0 if has_aud else None))
            year['gain_usdt'] += row['gain_usdt']
            if year['gain_aud'] is not None:
                year['gain_aud'] += row['gain_aud']
    return dict(saved=str(output), rows=len(rows), aud_values_included=has_aud,
                gain_by_tax_year=totals, open_positions_not_included=sorted(open_buys), note=NOTE)
