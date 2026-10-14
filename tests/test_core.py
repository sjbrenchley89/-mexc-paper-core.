import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import bot
from risk import Ledger
from strategies import evaluate

DAY = 86400
NOW = 1700000000
CONFIG = json.loads((bot.ROOT/'config.json').read_text())


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)/'paper.sqlite'
        self.config = copy.deepcopy(CONFIG)
        self.ledger = Ledger(self.path,self.config)

    def tearDown(self):
        self.tmp.cleanup()

    def buy(self, identifier='first', price=100, atr=1):
        return self.ledger.signal(identifier,'BTCUSDT','buy',price,atr,'demo','technical',NOW)

    def test_paper_balance_round_trip_includes_both_fees(self):
        self.assertTrue(self.buy()['accepted'])
        self.ledger.signal('exit','BTCUSDT','sell',100,1,'demo','technical',NOW+1)
        state = self.ledger.status(NOW+1)
        self.assertLess(state['equity'],50)
        self.assertEqual(state['positions'],{})
        self.assertEqual(len([e for e in state['events'] if e['kind']=='fill']),2)

    def test_duplicate_survives_process_restart(self):
        self.buy()
        reopened = Ledger(self.path,self.config)
        result = reopened.signal('first','BTCUSDT','buy',100,1,'demo','technical',NOW)
        self.assertEqual(result['reason'],'duplicate')

    def test_caps_cannot_be_raised_by_direct_config(self):
        self.config.update(total_cap=100000,position_cap=100000,risk_per_trade=100)
        result = self.buy()
        self.assertLessEqual(result['cost'],10)
        self.assertGreaterEqual(self.ledger.status(NOW)['cash'],40)

    def test_atr_sizing_decreases_for_more_volatile_asset(self):
        self.config['risk_per_trade'] = .001
        first = self.buy(atr=.5)['qty']
        with tempfile.TemporaryDirectory() as directory:
            other = Ledger(Path(directory)/'other.sqlite',self.config)
            second = other.signal('two','BTCUSDT','buy',100,2,'demo','technical',NOW)['qty']
        self.assertLess(second,first)

    def test_daily_halt_survives_restart_and_midnight(self):
        self.buy()
        self.ledger.mark({'BTCUSDT':1},NOW+1)
        self.assertTrue(self.ledger.status(NOW+1)['review_required'])
        with self.assertRaises(ValueError):
            self.ledger.control('review-rearm',NOW+1,'reviewed')
        restarted = Ledger(self.path,self.config)
        state = restarted.status(NOW+DAY)
        self.assertTrue(state['paused'])
        with self.assertRaises(ValueError):
            restarted.control('resume',NOW+DAY)
        restarted.control('review-rearm',NOW+DAY,'Reviewed gap and costs; keep paper mode.')
        self.assertFalse(restarted.status(NOW+DAY)['paused'])

    def test_whole_ledger_floor_independent_of_daily_baseline(self):
        def loss(state):
            state['cash']=24
            state['day_start']=24
        self.ledger.transact(NOW,loss)
        self.assertTrue(self.ledger.status(NOW)['floor_breached'])
        with self.assertRaises(ValueError):
            self.ledger.control('review-rearm',NOW+DAY,'reviewed')

    def test_protective_exit_allowed_after_kill(self):
        self.buy()
        self.ledger.control('kill',NOW+1)
        result = self.ledger.signal('exit','BTCUSDT','sell',100,1,'demo','technical',NOW+2)
        self.assertTrue(result['accepted'])
        self.assertEqual(self.ledger.status(NOW+2)['positions'],{})
        with self.assertRaises(ValueError):
            self.ledger.control('resume',NOW+2)

    def test_risk_limits_include_costs(self):
        result = self.buy()
        position = self.ledger.status(NOW)['positions']['BTCUSDT']
        proceeds = position['qty']*position['stop']*(1-self.config['slippage'])*(1-self.config['fee'])
        self.assertLessEqual(result['cost']-proceeds,50*self.config['risk_per_trade']+1e-9)

    def test_buys_reject_insufficient_liquidity(self):
        result = self.ledger.signal('tv','BTCUSDT','buy',100,1,'tradingview','technical',NOW,
            liquidity={'spread':.01,'depth':2000,'step':.001,'min_qty':.001,'min_notional':1})
        self.assertEqual(result['reason'],'liquidity_filter')

    def test_order_below_exchange_minimum_is_skipped(self):
        result = self.ledger.signal('tv','BTCUSDT','buy',100,1,'tradingview','technical',NOW,
            liquidity={'spread':.001,'depth':2000,'step':.001,'min_qty':.001,'min_notional':20})
        self.assertEqual(result['reason'],'exchange_minimum_or_step')

    def test_no_sell_of_unknown_holdings(self):
        result = self.ledger.signal('exit','BTCUSDT','sell',100,1,'demo','technical',NOW)
        self.assertEqual(result['reason'],'no_bot_position')

    def test_no_arbitrary_copy_or_futures_source(self):
        result = self.ledger.signal('copy','BTCUSDT','buy',100,1,'astral','technical',NOW)
        self.assertEqual(result['reason'],'unsupported_signal_source_or_action')


class InterfaceTests(unittest.TestCase):
    def test_live_configuration_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'config.json'
            config=copy.deepcopy(CONFIG)
            config['mode']='live'
            path.write_text(json.dumps(config))
            with self.assertRaisesRegex(ValueError,'PAPER ONLY'):
                bot.load_config(path)

    def test_bad_secret_cannot_fetch_market_data(self):
        payload = dict(secret='wrong',id='a',symbol='BTCUSDT',action='buy',strategy='technical',
                       timeframe='1h',sent_at=NOW,candle_time=NOW-3600)
        with patch('market.candles') as fetch:
            with self.assertRaises(PermissionError):
                bot.process_alert(payload,'test-secret',None,CONFIG,NOW)
            fetch.assert_not_called()

    def test_stale_alert_rejected_before_market_lookup(self):
        payload = dict(secret='test-secret',id='a',symbol='BTCUSDT',action='buy',strategy='technical',
                       timeframe='1h',sent_at=NOW-121,candle_time=NOW-3600)
        with patch('market.candles') as fetch:
            with self.assertRaises(ValueError):
                bot.process_alert(payload,'test-secret',None,CONFIG,NOW)
            fetch.assert_not_called()

    def test_exactly_two_builtin_strategies(self):
        bars=[dict(open=100,close=100,high=101,low=99,volume=100,time=i*3600) for i in range(100)]
        for strategy in ('technical','breakout'):
            action,indicators=evaluate(bars,strategy)
            self.assertEqual(action,'hold')
            self.assertGreater(indicators['atr'],0)
        with self.assertRaises(ValueError):
            evaluate(bars,'portal')

    def test_demo_does_not_touch_real_paper_ledger(self):
        result=bot.demo(CONFIG)
        self.assertTrue(result['synthetic_demo'])
        self.assertTrue(result['not_market_evidence'])


if __name__=='__main__':
    unittest.main()
