import copy
import csv
import json
import tempfile
import unittest
from pathlib import Path

import bot
import copytrade
import records
from risk import Ledger

NOW = 1700000000
HOUR = 3600
CONFIG = json.loads((bot.ROOT/'config.json').read_text())


class FakeMarket:
    def candles(self, symbol):
        return [dict(time=i*HOUR, open=100, high=100.5, low=99.5, close=100, volume=100) for i in range(100)]

    def price(self, symbol):
        return 100.0

    def liquidity(self, symbol):
        return dict(spread=.001, depth=5000, step=.0001, min_qty=.0001, min_notional=1)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.config = copy.deepcopy(CONFIG)
        self.ledger = Ledger(self.dir/'paper.sqlite', self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def buy(self, identifier='b1', at=NOW):
        return self.ledger.signal(identifier, 'BTCUSDT', 'buy', 100, 1, 'demo', 'technical', at)


class ConfigTests(Base):
    def load(self, **changes):
        self.config.update(changes)
        path = self.dir/'config.json'
        path.write_text(json.dumps(self.config))
        return bot.load_config(path)

    def test_shipped_config_is_option_c_with_slots_on(self):
        self.assertEqual((CONFIG['option'], CONFIG['news'], CONFIG['copy_trading']), ('C', True, True))
        self.assertEqual(self.load()['mode'], 'paper')

    def test_news_must_match_option_c(self):
        with self.assertRaisesRegex(ValueError, 'News veto'):
            self.load(news=False)
        with self.assertRaisesRegex(ValueError, 'News veto'):
            self.load(option='A', news=True)

    def test_tradovate_and_portal_still_refused(self):
        for key in ('tradovate', 'portal_strategies'):
            with self.assertRaisesRegex(ValueError, 'not implemented'):
                self.load(**{key: True})


class NewsVetoTests(Base):
    def test_veto_blocks_new_entry_but_not_exit(self):
        self.buy()
        self.ledger.news_block('BTCUSDT', 2, 'delisting notice', NOW)
        exit_result = self.ledger.signal('x', 'BTCUSDT', 'sell', 100, 1, 'demo', 'technical', NOW+1)
        self.assertTrue(exit_result['accepted'])
        self.assertEqual(self.buy('b2', NOW+2)['reason'], 'news_veto')

    def test_veto_expires(self):
        self.ledger.news_block('BTCUSDT', 1, 'maintenance', NOW)
        self.assertEqual(self.buy('b1', NOW+HOUR-1)['reason'], 'news_veto')
        self.assertTrue(self.buy('b2', NOW+HOUR+1)['accepted'])

    def test_wildcard_blocks_every_symbol(self):
        self.ledger.news_block('*', 1, 'exchange incident', NOW)
        self.assertEqual(self.buy()['reason'], 'news_veto')

    def test_veto_survives_restart(self):
        self.ledger.news_block('BTCUSDT', 5, 'hack report', NOW)
        reopened = Ledger(self.dir/'paper.sqlite', self.config)
        self.assertEqual(reopened.signal('b', 'BTCUSDT', 'buy', 100, 1, 'demo', 'technical', NOW+1)['reason'],
                         'news_veto')
        self.assertEqual(len(reopened.news_list(NOW+1)['active_blocks']), 1)

    def test_clear_needs_note_and_reopens_entries(self):
        self.ledger.news_block('BTCUSDT', 5, 'rumour', NOW)
        with self.assertRaises(ValueError):
            self.ledger.news_clear('BTCUSDT', ' ', NOW+1)
        self.assertEqual(self.ledger.news_clear('BTCUSDT', 'Checked official notice; false', NOW+1)['removed'], 1)
        self.assertTrue(self.buy('b', NOW+2)['accepted'])

    def test_invalid_blocks_refused(self):
        for symbol, hours, reason in (('ETHUSDT', 1, 'x'), ('BTCUSDT', 0, 'x'), ('BTCUSDT', 169, 'x'),
                                      ('BTCUSDT', float('nan'), 'x'), ('BTCUSDT', 1, '  ')):
            with self.assertRaises(ValueError):
                self.ledger.news_block(symbol, hours, reason, NOW)

    def test_off_when_news_disabled(self):
        config = copy.deepcopy(self.config)
        config.update(option='A', news=False)
        ledger = Ledger(self.dir/'a.sqlite', config)
        with self.assertRaises(ValueError):
            ledger.news_block('BTCUSDT', 1, 'x', NOW)

    def test_scanner_source_allowed_in_option_c(self):
        result = self.ledger.signal('s', 'BTCUSDT', 'buy', 100, 1, 'scanner', 'breakout', NOW,
                                    liquidity=dict(spread=.001, depth=5000, step=.0001, min_qty=.0001, min_notional=1))
        self.assertTrue(result['accepted'])


class CopyTests(Base):
    def event(self, identifier='c1', age=30, **changes):
        event = dict(provider='fomo', id=identifier, symbol='BTCUSDT', action='buy', source_time=NOW-age)
        event.update(changes)
        return event

    def process(self, event, now=NOW):
        return copytrade.process_event(event, self.ledger, self.config, now, FakeMarket())

    def test_all_providers_disconnected(self):
        status = copytrade.status(self.config)
        self.assertFalse(status['automatic_copying'])
        self.assertTrue(all(not p['connected'] for p in status['providers']))
        self.assertEqual(copytrade.adapters()['fomo'].poll(), [])

    def test_copy_buy_goes_through_caps_and_records_latency(self):
        result = self.process(self.event())
        self.assertTrue(result['accepted'])
        self.assertLessEqual(result['cost'], 10)
        fill = [e for e in self.ledger.status(NOW)['events'] if e['kind'] == 'fill'][0]
        self.assertEqual((fill['source'], fill['strategy'], fill['latency']), ('copy', 'copy:fomo', 30))

    def test_stale_duplicate_and_halted_copies_rejected(self):
        self.assertEqual(self.process(self.event('old', age=121))['reason'], 'stale_or_future_signal')
        self.assertTrue(self.process(self.event('one'))['accepted'])
        self.assertEqual(self.process(self.event('one'))['reason'], 'duplicate')
        self.ledger.control('kill', NOW+1)
        self.assertEqual(self.process(self.event('two'), NOW+2)['reason'], 'safety_halt')

    def test_news_veto_applies_to_copy_entries(self):
        self.ledger.news_block('BTCUSDT', 1, 'hack report', NOW)
        self.assertEqual(self.process(self.event())['reason'], 'news_veto')

    def test_bad_events_refused(self):
        for changes in (dict(provider='other'), dict(symbol='ETHUSDT'), dict(action='short'),
                        dict(source_time=float('inf')), dict(id='')):
            with self.assertRaises(ValueError):
                self.process(self.event(**changes))
        extra = self.event()
        extra['price'] = 1
        with self.assertRaises(ValueError):
            self.process(extra)

    def test_copy_disabled_in_config(self):
        self.config['copy_trading'] = False
        with self.assertRaises(ValueError):
            self.process(self.event())
        direct = self.ledger.signal('d', 'BTCUSDT', 'buy', 100, 1, 'copy', 'copy:fomo', NOW, NOW)
        self.assertEqual(direct['reason'], 'copy_disabled')

    def test_ledger_requires_source_time_for_copy(self):
        result = self.ledger.signal('d', 'BTCUSDT', 'buy', 100, 1, 'copy', 'copy:fomo', NOW)
        self.assertEqual(result['reason'], 'copy_requires_source_time')

    def test_stale_copy_sell_cannot_close_a_position(self):
        self.assertTrue(self.process(self.event('buy1'))['accepted'])
        result = self.process(self.event('sell1', action='sell', age=3600), NOW+1)
        self.assertEqual(result['reason'], 'stale_or_future_signal')
        self.assertIn('BTCUSDT', self.ledger.status(NOW+2)['positions'])
        fresh = self.process(self.event('sell2', action='sell', age=5), NOW+2)
        self.assertTrue(fresh['accepted'])

    def test_copy_sell_needs_a_source_time(self):
        self.process(self.event('buy1'))
        result = self.ledger.signal('s', 'BTCUSDT', 'sell', 100, 1, 'copy', 'copy:fomo', NOW+1)
        self.assertEqual(result['reason'], 'copy_requires_source_time')

    def test_report_counts_latency_pnl_and_rejections(self):
        self.process(self.event('b', age=500))
        self.process(self.event('a', age=20))
        self.process(self.event('s', action='sell', age=1), NOW+5)
        by = copytrade.report(self.ledger.status(NOW+5))['by_provider']
        self.assertEqual((by['fomo']['entries'], by['fomo']['closed_trades']), (1, 1))
        self.assertEqual(by['fomo']['avg_latency_seconds'], 20)
        self.assertEqual(by['fomo']['rejections'], {'stale_or_future_signal': 1})
        self.assertLess(by['fomo']['net_pnl_usdt'], 0)
        self.assertEqual(by['astral']['entries'], 0)


class RecordsTests(Base):
    def trade(self):
        self.buy()
        self.ledger.signal('x', 'BTCUSDT', 'sell', 101, 1, 'demo', 'technical', NOW+HOUR)
        return self.ledger.status(NOW+HOUR)

    def read(self, path):
        with open(path, newline='') as file:
            return list(csv.DictReader(file))

    def test_tax_year_boundary_uses_aest(self):
        before = 1782828000-1
        self.assertEqual(records.tax_year(before), '2025-26')
        self.assertEqual(records.tax_year(before+1), '2026-27')
        self.assertEqual(records.tax_year(NOW), '2023-24')

    def test_flat_rate_converts_each_row_and_cost_base_uses_buy_rate(self):
        state = self.trade()
        output = self.dir/'out.csv'
        summary = records.export(state, output, flat_rate=1.5)
        rows = self.read(output)
        self.assertEqual([r['type'] for r in rows], ['buy', 'sell'])
        self.assertEqual({r['mode'] for r in rows}, {'paper'})
        sell = rows[1]
        self.assertAlmostEqual(float(sell['gain_aud']), float(sell['gain_usdt'])*1.5)
        self.assertAlmostEqual(float(sell['gross_aud']), float(sell['gross_usdt'])*1.5)
        self.assertEqual(sell['held_over_12_months'], 'no')
        self.assertTrue(summary['aud_values_included'])
        self.assertIn('2023-24', summary['gain_by_tax_year'])

    def test_daily_rates_file_changes_cost_base(self):
        state = self.trade()
        rates = self.dir/'rates.csv'
        rates.write_text('date,aud_per_usdt\n2023-11-14,1.50\n2023-11-15,1.60\n')
        rows = self.read(self.export(state, rates=rates))
        self.assertEqual([r['aud_per_usdt'] for r in rows], ['1.5', '1.5'])

    def export(self, state, **kwargs):
        output = self.dir/'o.csv'
        records.export(state, output, kwargs.get('rates'), kwargs.get('flat'))
        return output

    def test_rate_on_a_different_day_used_for_sale(self):
        self.buy()
        later = NOW+2*86400
        self.ledger.signal('x', 'BTCUSDT', 'sell', 100, 1, 'demo', 'technical', later)
        rates = self.dir/'rates.csv'
        rates.write_text('date,aud_per_usdt\n2023-11-14,1.50\n2023-11-16,1.60\n')
        sell = self.read(self.export(self.ledger.status(later), rates=rates))[1]
        self.assertAlmostEqual(float(sell['cost_base_aud']), float(sell['cost_base_usdt'])*1.5)
        self.assertAlmostEqual(float(sell['proceeds_aud']), float(sell['proceeds_usdt'])*1.6)

    def test_missing_rate_date_refused_without_guessing(self):
        self.buy()
        self.ledger.signal('x', 'BTCUSDT', 'sell', 100, 1, 'demo', 'technical', NOW+3*HOUR)
        state = self.ledger.status(NOW+3*HOUR)
        rates = self.dir/'rates.csv'
        rates.write_text('date,aud_per_usdt\n2023-11-14,1.50\n')
        with self.assertRaisesRegex(ValueError, '2023-11-15'):
            records.export(state, self.dir/'o.csv', rates)
        self.assertFalse((self.dir/'o.csv').exists())

    def test_no_rate_leaves_aud_blank_and_says_so(self):
        output = self.dir/'o.csv'
        summary = records.export(self.trade(), output)
        self.assertFalse(summary['aud_values_included'])
        self.assertEqual({r['gross_aud'] for r in self.read(output)}, {''})

    def test_bad_rates_and_overwrite_refused(self):
        state = self.trade()
        rates = self.dir/'rates.csv'
        for body in ('date,aud_per_usdt\n2023-11-14,0\n', 'date,aud_per_usdt\n2023-11-14,nan\n',
                     'date,aud_per_usdt\nnot-a-date,1.5\n', 'date,aud_per_usdt\n2023-11-14,1.5\n2023-11-14,1.6\n',
                     'when,rate\n2023-11-14,1.5\n'):
            rates.write_text(body)
            with self.assertRaises(ValueError):
                records.export(state, self.dir/'x.csv', rates)
        with self.assertRaises(ValueError):
            records.export(state, self.dir/'x.csv', flat_rate=-1)
        with self.assertRaises(ValueError):
            records.export(state, self.dir/'x.csv', rates, 1.5)
        records.export(state, self.dir/'ok.csv', flat_rate=1.5)
        with self.assertRaises(FileExistsError):
            records.export(state, self.dir/'ok.csv', flat_rate=1.5)

    def test_open_position_reported_not_exported_as_sale(self):
        self.buy()
        summary = records.export(self.ledger.status(NOW), self.dir/'o.csv', flat_rate=1.5)
        self.assertEqual(summary['open_positions_not_included'], ['BTCUSDT'])
        self.assertEqual(summary['gain_by_tax_year'], {})


if __name__ == '__main__':
    unittest.main()
