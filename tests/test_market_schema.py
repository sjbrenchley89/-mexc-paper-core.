"""Parsing checks against payload shapes published in MEXC's Spot V3 documentation."""
import time
import unittest
from unittest.mock import patch

import market

MEXC_INFO = {'symbol': 'METALUSDT', 'status': '1', 'baseAssetPrecision': 2, 'quotePrecision': 5,
             'quoteAssetPrecision': 5, 'baseSizePrecision': '0.1', 'quoteAmountPrecision': '1',
             'maxQuoteAmount': '2000000', 'isSpotTradingAllowed': True, 'permissions': ['SPOT'],
             'filters': [{'filterType': 'PERCENT_PRICE_BY_SIDE', 'bidMultiplierUp': '5'}]}
TICKER = {'symbol': 'BTCUSDT', 'priceChange': '184.34', 'priceChangePercent': '0.00400048',
          'prevClosePrice': '46079.37', 'lastPrice': '46263.71', 'openPrice': '46079.37',
          'volume': '1732.461487', 'quoteVolume': None}


class SymbolRuleTests(unittest.TestCase):
    def test_documented_mexc_fields_give_rules(self):
        rules = market.symbol_rules(MEXC_INFO)
        self.assertEqual(rules, dict(step=0.1, min_qty=0.1, min_notional=1.0))

    def test_finer_base_precision_sets_step(self):
        info = dict(MEXC_INFO, baseAssetPrecision=6, baseSizePrecision='0.000001')
        self.assertAlmostEqual(market.symbol_rules(info)['step'], 1e-6)

    def test_step_is_not_finer_than_stated_minimum(self):
        info = dict(MEXC_INFO, baseAssetPrecision=8, baseSizePrecision='0.001')
        self.assertAlmostEqual(market.symbol_rules(info)['step'], 0.001)

    def test_binance_style_filters_still_accepted(self):
        info = dict(MEXC_INFO, filters=[{'filterType': 'LOT_SIZE', 'stepSize': '0.0001', 'minQty': '0.0002'},
                                        {'filterType': 'NOTIONAL', 'minNotional': '5'}])
        self.assertEqual(market.symbol_rules(info), dict(step=0.0001, min_qty=0.0002, min_notional=5.0))

    def test_offline_paused_or_api_disabled_refused(self):
        for changes in (dict(status='2'), dict(status='3'), dict(isSpotTradingAllowed=False)):
            with self.assertRaisesRegex(ValueError, 'not enabled'):
                market.symbol_rules(dict(MEXC_INFO, **changes))
        missing = {k: v for k, v in MEXC_INFO.items() if k != 'isSpotTradingAllowed'}
        with self.assertRaisesRegex(ValueError, 'not enabled'):
            market.symbol_rules(missing)

    def test_missing_rules_fail_closed(self):
        info = {k: v for k, v in MEXC_INFO.items() if k != 'baseSizePrecision'}
        with self.assertRaisesRegex(ValueError, 'rules absent'):
            market.symbol_rules(info)

    def test_trading_status(self):
        with patch('market.get', return_value={'symbols': [MEXC_INFO]}):
            self.assertTrue(market.trading_status('METALUSDT')['ok'])
        with patch('market.get', return_value={'symbols': [dict(MEXC_INFO, status='2')]}):
            self.assertFalse(market.trading_status('METALUSDT')['ok'])


class TickerTests(unittest.TestCase):
    def test_change_is_derived_in_percent_not_taken_from_fraction(self):
        stats = market.ticker_stats(TICKER)
        self.assertAlmostEqual(stats['change_pct'], 0.4000, places=3)

    def test_null_quote_volume_is_estimated_from_base_volume(self):
        self.assertAlmostEqual(market.ticker_stats(TICKER)['quote_volume'], 1732.461487*46263.71)
        given = market.ticker_stats(dict(TICKER, quoteVolume='5000000'))
        self.assertEqual(given['quote_volume'], 5000000.0)


class RequestShapeTests(unittest.TestCase):
    def test_hourly_candles_request_60m(self):
        seen = {}
        def fake_get(path, **params):
            seen.update(params)
            return []
        with patch('market.get', fake_get):
            market.candles('BTCUSDT')
        self.assertEqual(seen['interval'], '60m')

    def test_scan_finds_candidate_from_documented_shapes(self):
        now_ms = int(time.time()*1000)
        bars = [[now_ms-(200-i)*3600000, '100', '100.5', '99.5', '100', '100', now_ms-(200-i)*3600000+3599999, '10000']
                for i in range(100)]
        ticker = dict(TICKER, lastPrice='103', openPrice='100', volume='20000', quoteVolume=None)
        book = {'lastUpdateId': 1, 'bids': [['102.95', '1000']], 'asks': [['103.0', '1000']]}
        def fake_get(path, **params):
            if path == '/api/v3/ticker/24hr':
                return [ticker]
            if path == '/api/v3/klines':
                self.assertEqual(params['interval'], '60m')
                return bars
            if path == '/api/v3/depth':
                return book
            if path == '/api/v3/exchangeInfo':
                return {'symbols': [dict(MEXC_INFO, symbol='BTCUSDT')]}
            raise AssertionError(path)
        with patch('market.get', fake_get):
            found = market.scan(['BTCUSDT'])
        self.assertEqual([c['symbol'] for c in found], ['BTCUSDT'])
        self.assertAlmostEqual(found[0]['change_pct'], 3.0)
        self.assertEqual(found[0]['liquidity']['min_notional'], 1.0)


if __name__ == '__main__':
    unittest.main()
