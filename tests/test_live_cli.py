import contextlib
import copy
import http.client
import io
import json
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

import bot
import live
from risk import Ledger

NOW = 1700000000
CONFIG = json.loads((bot.ROOT/'config.json').read_text())


class StatusMarket:
    def __init__(self, ok=True, fail=False):
        self.ok, self.fail = ok, fail

    def trading_status(self, symbol):
        if self.fail:
            raise RuntimeError('network')
        return dict(ok=self.ok, status='1' if self.ok else '2')


class ConfirmationTests(unittest.TestCase):
    def test_exact_phrase_required(self):
        with contextlib.redirect_stdout(io.StringIO()):
            bot.confirm_live(lambda prompt: live.PHRASE)
            for typed in ('yes', live.PHRASE.lower(), '', live.PHRASE+' now'):
                with self.assertRaises(ValueError):
                    bot.confirm_live(lambda prompt, typed=typed: typed)

    def test_no_terminal_means_no_live(self):
        def no_input(prompt):
            raise EOFError
        with contextlib.redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
            bot.confirm_live(no_input)


class ConfigTests(unittest.TestCase):
    def load(self, **changes):
        config = copy.deepcopy(CONFIG)
        config.update(changes)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'config.json'
            path.write_text(json.dumps(config))
            return bot.load_config(path)

    def test_shipped_live_confirmations_are_false(self):
        self.assertEqual(CONFIG['live'], dict(mexc_eligibility_confirmed=False, trade_only_key_confirmed=False))

    def test_live_block_is_validated(self):
        self.load(live=dict(mexc_eligibility_confirmed=True, trade_only_key_confirmed=True))
        for bad in ({'mexc_eligibility_confirmed': 'yes'}, {'surprise': True}, [True]):
            with self.assertRaisesRegex(ValueError, 'live must contain'):
                self.load(live=bad)

    def test_config_mode_stays_paper(self):
        with self.assertRaisesRegex(ValueError, 'PAPER ONLY'):
            self.load(mode='live')


class AutoVetoTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ledger = Ledger(Path(self.tmp.name)/'p.sqlite', CONFIG)

    def tearDown(self):
        self.tmp.cleanup()

    def test_offline_symbol_gets_a_one_hour_automatic_block_once(self):
        self.assertEqual(bot.auto_status_veto(self.ledger, CONFIG, StatusMarket(ok=False), NOW), ['BTCUSDT'])
        self.assertEqual(bot.auto_status_veto(self.ledger, CONFIG, StatusMarket(ok=False), NOW+300), [])
        blocks = self.ledger.news_list(NOW+300)['active_blocks']
        self.assertEqual(len(blocks), 1)
        self.assertTrue(blocks[0]['reason'].startswith('AUTO:'))
        result = self.ledger.signal('s', 'BTCUSDT', 'buy', 100, 1, 'demo', 'technical', NOW+301)
        self.assertEqual(result['reason'], 'news_veto')

    def test_block_is_renewed_before_it_expires(self):
        bot.auto_status_veto(self.ledger, CONFIG, StatusMarket(ok=False), NOW)
        self.assertEqual(bot.auto_status_veto(self.ledger, CONFIG, StatusMarket(ok=False), NOW+2000), ['BTCUSDT'])

    def test_online_or_unreachable_creates_no_block(self):
        self.assertEqual(bot.auto_status_veto(self.ledger, CONFIG, StatusMarket(ok=True), NOW), [])
        self.assertEqual(bot.auto_status_veto(self.ledger, CONFIG, StatusMarket(fail=True), NOW), [])
        self.assertEqual(self.ledger.news_list(NOW)['active_blocks'], [])


class CommandTests(unittest.TestCase):
    def run_cli(self, *argv):
        out = io.StringIO()
        with patch('sys.argv', ['bot.py', *argv]), contextlib.redirect_stdout(out):
            try:
                bot.main()
                code = 0
            except SystemExit as exc:
                code = exc.code
        return code, out.getvalue()

    def test_live_flag_refused_for_commands_that_could_start_trading(self):
        with tempfile.TemporaryDirectory() as directory:
            for command in (['start'], ['demo'], ['setup'], ['copy-signal', '--provider', 'fomo', '--id', 'x',
                                                           '--symbol', 'BTCUSDT', '--action', 'buy', '--seconds-ago', '1']):
                code, _ = self.run_cli('--live', '--state', str(Path(directory)/'live.sqlite'), *command)
                self.assertEqual(code, 1, command)

    def test_live_status_uses_live_ledger_and_is_unsynced(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory)/'live.sqlite')
            code, out = self.run_cli('--live', '--state', path, 'status')
            data = json.loads(out)
            self.assertEqual((code, data['mode'], data['synced']), (0, 'live', False))
            self.assertFalse(data['review_required'])

    def test_live_kill_marks_halt_in_live_ledger_only(self):
        with tempfile.TemporaryDirectory() as directory:
            live_path, paper_path = str(Path(directory)/'live.sqlite'), str(Path(directory)/'paper.sqlite')
            self.run_cli('--live', '--state', live_path, 'kill')
            code, out = self.run_cli('--state', paper_path, 'status')
            self.assertFalse(json.loads(out)['killed'])
            code, out = self.run_cli('--live', '--state', live_path, 'status')
            self.assertTrue(json.loads(out)['killed'])

    def test_preflight_without_keys_fails_closed_and_trades_nothing(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {}, clear=True):
            code, out = self.run_cli('--state', str(Path(directory)/'paper.sqlite'), 'live-preflight')
            data = json.loads(out)
            self.assertEqual(code, 0)
            self.assertFalse(data['ready'])
            self.assertTrue(data['nothing_was_traded'])
            failed = {c['name'] for c in data['checks'] if not c['ok']}
            self.assertIn('API keys present in environment', failed)
            self.assertIn('Paper trial length', failed)

    def test_doctor_reports_progress_without_network_or_trading(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {}, clear=True):
            code, out = self.run_cli('--state', str(Path(directory)/'paper.sqlite'), 'doctor')
            data = json.loads(out)
            self.assertEqual(code, 0)
            self.assertFalse(data['ready_for_preflight'])
            self.assertIn('makes no contact with MEXC', data['note'])
            self.assertTrue(any(s['name'] == 'MEXC_API_KEY and MEXC_API_SECRET set' and not s['ok'] for s in data['steps']))

    def test_doctor_is_refused_with_the_live_flag(self):
        with tempfile.TemporaryDirectory() as directory:
            code, _ = self.run_cli('--live', '--state', str(Path(directory)/'live.sqlite'), 'doctor')
            self.assertEqual(code, 1)

    def test_live_start_refuses_when_preflight_fails(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict('os.environ', {'WEBHOOK_SECRET': 'x'*40}, clear=True):
            code, out = self.run_cli('--state', str(Path(directory)/'paper.sqlite'), 'live-start')
            self.assertEqual(code, 1)
            self.assertIn('"ready": false', out)
            self.assertFalse((bot.ROOT/'live.sqlite').exists())


class ServerModeTests(unittest.TestCase):
    def request(self, ledger, method, path, body=None):
        server = ThreadingHTTPServer(('127.0.0.1', 0), bot.make_handler(ledger, 'secret-token-0123456789'))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        try:
            connection = http.client.HTTPConnection('127.0.0.1', server.server_address[1], timeout=5)
            connection.request(method, path, body=json.dumps(body) if body is not None else None)
            reply = connection.getresponse()
            return reply.status, json.loads(reply.read())
        finally:
            server.shutdown()
            server.server_close()

    def test_health_reports_the_real_mode(self):
        with tempfile.TemporaryDirectory() as directory:
            paper = Ledger(Path(directory)/'p.sqlite', CONFIG)
            real = Ledger(Path(directory)/'l.sqlite', CONFIG, live=True)
            self.assertEqual(self.request(paper, 'GET', '/health'), (200, {'mode': 'paper', 'live_enabled': False}))
            self.assertEqual(self.request(real, 'GET', '/health'), (200, {'mode': 'live', 'live_enabled': True}))

    def test_webhook_acknowledgement_reports_the_real_mode(self):
        alert = dict(secret='secret-token-0123456789', id='a', symbol='BTCUSDT', action='buy')
        with tempfile.TemporaryDirectory() as directory:
            real = Ledger(Path(directory)/'l.sqlite', CONFIG, live=True)
            status, data = self.request(real, 'POST', '/webhook', alert)
            self.assertEqual((status, data['mode'], data['filled']), (202, 'live', False))
            status, _ = self.request(real, 'POST', '/webhook', dict(alert, secret='wrong-wrong-wrong-wrong-0000'))
            self.assertEqual(status, 401)


if __name__ == '__main__':
    unittest.main()
