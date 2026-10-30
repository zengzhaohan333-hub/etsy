"""Four-market coordinator fixtures: no credentials or real exchange traffic."""
import contextlib
import hashlib
import io
import json
import tempfile
import unittest
import uuid
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

from okx_bot import demo, portfolio
from okx_bot.portfolio_config import PortfolioConfig
from okx_bot.core import CrossStrategy
from okx_bot.okx import AmbiguousOrderError, Candle, Credentials, Instrument, OkxError


MARKETS = ('BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'SOL-USDT-SWAP', 'XRP-USDT-SWAP')
INTERVAL = 300_000
NOW = 1_800_000


def config(**changes):
    return PortfolioConfig(fast_window=2, slow_window=3, **changes)


def candle(ts, price):
    value = D(str(price))
    return Candle(ts, value, value * D('1.001'), value * D('.999'), value)


class NoSleepStop:
    def __init__(self):
        self.stopped = False
        self.waits = []
        self.on_wait = None

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.waits.append(seconds)
        if self.on_wait:
            self.on_wait(seconds)
        return self.stopped


class AlwaysLong:
    """Emit on every update, verifying that warm-up signals cannot place orders."""
    def __init__(self, fast, slow):
        pass

    def update(self, bar):
        return 'long'


class FakePortfolioClient:
    def __init__(self, directory):
        self.state_dir = Path(directory)
        self.credentials = Credentials('test-' + uuid.uuid4().hex, 'test-secret', 'test-passphrase')
        self.now = NOW
        self.equity = D('5000')
        self.balance_currency = 'USDT'
        self.add_new = True
        self.real_crossing = False
        self.calls = []
        self.place_calls = []
        self.leverage_calls = []
        self.candle_calls = {market: 0 for market in MARKETS}
        self.position_rows = {market: [] for market in MARKETS}
        self.algo_rows = {market: [] for market in MARKETS}
        self.order_rows = {market: [] for market in MARKETS}
        self.orders = {}
        self.prices = dict(zip(MARKETS, map(D, ('100', '10', '1', '.1'))))
        self.units = dict(zip(MARKETS, map(D, ('.01', '.1', '1', '10'))))
        self.stale_marks = False
        self.fail_ambiguous_market = None
        self.fail_instrument_market = None
        self.hide_protection_market = None
        self.fail_leverage = False
        self.after_place = None

    def account_config(self):
        self.calls.append(('account_config', None))
        return {'posMode': 'net_mode', 'acctLv': '2'}

    def server_time(self):
        self.calls.append(('server_time', None))
        return self.now

    def instrument(self, inst_id):
        self.calls.append(('instrument', inst_id))
        if inst_id == self.fail_instrument_market:
            raise OkxError('OKX rejected request (code 50110)', api_code='50110', category='api')
        return Instrument(inst_id, self.units[inst_id], D('.01'), D('.01'), D('1000000'),
                          self.prices[inst_id] / D('10000'), inst_id.split('-')[0])

    def positions(self, inst_id=None):
        self.calls.append(('positions', inst_id))
        if inst_id is None:
            return [row for market in MARKETS for row in self.position_rows[market]]
        return list(self.position_rows[inst_id])

    def pending_orders(self, inst_id=None):
        self.calls.append(('pending_orders', inst_id))
        if inst_id is None:
            return [row for market in MARKETS for row in self.order_rows[market]]
        return list(self.order_rows[inst_id])

    def pending_algo_orders(self, inst_id=None):
        self.calls.append(('pending_algo_orders', inst_id))
        if inst_id == self.hide_protection_market:
            return []
        if inst_id is None:
            return [row for market in MARKETS for row in self.algo_rows[market]]
        return list(self.algo_rows[inst_id])

    def candles(self, inst_id, bar, limit):
        self.calls.append(('candles', inst_id))
        assert bar == '5m'
        self.candle_calls[inst_id] += 1
        price = self.prices[inst_id]
        history = [candle(NOW - (5 - i) * INTERVAL, price * factor)
                   for i, factor in enumerate(map(D, ('1.03', '1.02', '1.01', '1.02')))] if self.real_crossing else [
                       candle(NOW - (5 - i) * INTERVAL, price) for i in range(4)]
        if self.candle_calls[inst_id] > 1 and self.add_new:
            ts = NOW - INTERVAL
            while ts <= self.now - INTERVAL:
                value = price * D('1.04') if self.real_crossing else price
                history.append(candle(ts, value))
                ts += INTERVAL
        return history

    def current_notional(self):
        return sum((abs(D(row['pos'])) * self.units[market] * self.prices[market]
                    for market in MARKETS for row in self.position_rows[market]), D(0))

    def balances(self):
        self.calls.append(('balances', None))
        result = {'equity': self.equity,
                  'available': max(D(0), self.equity - self.current_notional())}
        if self.balance_currency is not None:
            result['currency'] = self.balance_currency
        return result

    def mark_price(self, inst_id):
        self.calls.append(('mark_price', inst_id))
        return self.prices[inst_id], self.now - 30_001 if self.stale_marks else self.now

    def set_leverage(self, inst_id, leverage):
        self.calls.append(('set_leverage', inst_id))
        self.leverage_calls.append((inst_id, leverage))
        if self.fail_leverage:
            raise OkxError('safe transport error', category='transport')
        assert leverage == '1'
        return {'lever': '1', 'mgnMode': 'isolated'}

    def get_leverage(self, inst_id):
        self.calls.append(('get_leverage', inst_id))
        return [{'instId': inst_id, 'mgnMode': 'isolated', 'lever': '1'}]

    def place_market_order(self, inst_id, side, size, client_id, *, stop_loss, take_profit):
        self.calls.append(('place_market_order', inst_id))
        journal = json.loads((self.state_dir / 'demo-journal.json').read_text())
        intent = next(row for row in journal['orders'] if row['client_id'] == client_id)
        assert intent['status'] == 'intent'
        assert intent['instrument'] == inst_id
        self.place_calls.append({'instrument': inst_id, 'side': side, 'size': size,
                                 'client_id': client_id, 'notional': size * self.units[inst_id] * self.prices[inst_id]})
        self.orders[client_id] = {'instId': inst_id, 'clOrdId': client_id, 'ordId': str(len(self.place_calls)),
                                  'state': 'filled', 'accFillSz': str(size)}
        self.position_rows[inst_id] = [{'instId': inst_id, 'mgnMode': 'isolated', 'posSide': 'net',
                                      'lever': '1', 'pos': str(size if side == 'buy' else -size),
                                      'avgPx': str(self.prices[inst_id])}]
        self.algo_rows[inst_id] = [{'instId': inst_id, 'algoClOrdId': client_id + 'a', 'sz': str(size),
                                  'side': 'sell' if side == 'buy' else 'buy', 'posSide': 'net', 'tdMode': 'isolated',
                                  'slTriggerPx': str(stop_loss), 'tpTriggerPx': str(take_profit),
                                  'slOrdPx': '-1', 'tpOrdPx': '-1', 'slTriggerPxType': 'mark', 'tpTriggerPxType': 'mark'}]
        if self.after_place:
            self.after_place(inst_id)
        if inst_id == self.fail_ambiguous_market:
            raise AmbiguousOrderError('Order response unavailable; inspect exchange', category='transport')
        return {'ordId': str(len(self.place_calls)), 'clOrdId': client_id, 'sCode': '0'}

    def query_order(self, inst_id, client_id):
        self.calls.append(('query_order', inst_id))
        return self.orders[client_id]


class PortfolioTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name)
        self.client = FakePortfolioClient(self.state_dir)
        self.stop = NoSleepStop()
        for target in ('okx_bot.portfolio._now_ms', 'okx_bot.demo._now_ms'):
            clock = patch(target, side_effect=lambda: self.client.now)
            clock.start()
            self.addCleanup(clock.stop)
        strategy = patch('okx_bot.portfolio.CrossStrategy', AlwaysLong)
        strategy.start()
        self.addCleanup(strategy.stop)

    def run_bot(self, cycles=1, profile=None):
        return portfolio.run_portfolio(profile or config(), self.state_dir, self.stop,
                                       poll_seconds=.001, client=self.client, max_cycles=cycles)

    def journal(self):
        return json.loads((self.state_dir / 'demo-journal.json').read_text())

    def protected_eth_read_failure(self, *, fail_once):
        """Complete ETH entry, then fail its protection GET next 5m cycle."""
        class OnlyEth:
            def __init__(self, fast, slow):
                pass
            def update(self, bar):
                return 'long' if bar.close == D(10) else None
        original_algos = self.client.pending_algo_orders
        failures = []
        snapshots = []
        original_save = portfolio._save
        def unavailable(inst_id):
            if inst_id == MARKETS[1] and self.client.now > NOW and (not fail_once or not failures):
                failures.append(inst_id)
                raise OkxError('OKX rejected request (code 50001)', api_code='50001', category='api')
            return original_algos(inst_id)
        def save(path, contents):
            original_save(path, contents)
            if path.name == 'status.json':
                snapshots.append(json.loads(json.dumps(contents)))
        self.client.pending_algo_orders = unavailable
        self.stop.on_wait = lambda seconds: setattr(self.client, 'now', self.client.now + INTERVAL) if seconds == .001 else None
        with patch('okx_bot.portfolio.CrossStrategy', OnlyEth), patch('okx_bot.portfolio._save', side_effect=save):
            result = self.run_bot(cycles=2)
        return result, failures, snapshots, original_algos

    def test_four_markets_open_independently_with_shared_and_per_market_caps(self):
        result = self.run_bot()
        self.assertFalse(result['halted'], result)
        self.assertEqual(result['mode'], 'demo')
        self.assertEqual(result['scope'], 'portfolio')
        self.assertEqual(set(result['markets']), set(MARKETS))
        self.assertEqual(result['entries'], 4)
        self.assertEqual({row['instrument'] for row in self.client.place_calls}, set(MARKETS))
        self.assertEqual(len(self.client.place_calls), 4)
        self.assertEqual(len(self.client.leverage_calls), 4)
        self.assertLessEqual(sum(row['notional'] for row in self.client.place_calls), D(800))
        for row in self.client.place_calls:
            self.assertLessEqual(row['notional'], D(200))
            self.assertEqual(row['size'] % D('.01'), 0)
            self.assertEqual(result['markets'][row['instrument']]['entries'], 1)
            self.assertEqual(result['markets'][row['instrument']]['position']['side'], 'long')
        self.assertEqual(len({row['client_id'] for row in self.client.place_calls}), 4)
        self.assertEqual({row['instrument'] for row in self.journal()['orders']}, set(MARKETS))
        self.assertTrue(all(row['status'] == 'protected' for row in self.journal()['orders']))

    def test_real_sma_instances_form_independent_signals_for_each_symbol(self):
        self.client.real_crossing = True
        with patch('okx_bot.portfolio.CrossStrategy', CrossStrategy):
            result = self.run_bot()
        self.assertFalse(result['halted'], result)
        self.assertEqual(result['entries'], 4)

    def test_smaller_configured_caps_apply_to_all_four_markets(self):
        result = self.run_bot(profile=config(max_notional=D(50), max_total_notional=D(200)))
        self.assertFalse(result['halted'], result)
        self.assertEqual(result['entries'], 4)
        self.assertTrue(all(row['notional'] <= D(50) for row in self.client.place_calls))
        self.assertLessEqual(self.client.current_notional(), D(200))

    def test_historical_alignment_signals_never_submit(self):
        self.client.add_new = False
        result = self.run_bot(cycles=2)
        self.assertFalse(result['halted'], result)
        self.assertEqual(result['entries'], 0)
        self.assertEqual(self.client.place_calls, [])

    def test_last_market_existing_position_prevents_all_mutations(self):
        self.client.position_rows[MARKETS[-1]] = [{'instId': MARKETS[-1], 'pos': '1', 'avgPx': '.1'}]
        result = self.run_bot()
        self.assertTrue(result['halted'])
        self.assertEqual(self.client.leverage_calls, [])
        self.assertEqual(self.client.place_calls, [])

    def test_missing_or_non_usdt_account_currency_blocks_all_startup_mutations(self):
        for currency in (None, 'USDC', 'BTC'):
            with self.subTest(currency=currency):
                self.client.balance_currency = currency
                result = self.run_bot()
                self.assertTrue(result['halted'])
                self.assertEqual(self.client.leverage_calls, [])
                self.assertEqual(self.client.place_calls, [])

    def test_last_market_pending_order_or_algo_prevents_all_mutations(self):
        for rows in (self.client.order_rows, self.client.algo_rows):
            with self.subTest(kind='ordinary' if rows is self.client.order_rows else 'algo'):
                rows[MARKETS[-1]] = [{'external': True}]
                result = self.run_bot()
                self.assertTrue(result['halted'])
                self.assertEqual(self.client.leverage_calls, [])
                self.assertEqual(self.client.place_calls, [])
                rows[MARKETS[-1]] = []

    def test_bad_instrument_on_last_market_prevents_all_mutations(self):
        self.client.fail_instrument_market = MARKETS[-1]
        result = self.run_bot()
        self.assertTrue(result['halted'])
        self.assertEqual(self.client.leverage_calls, [])
        self.assertEqual(self.client.place_calls, [])

    def assert_safe_failure_output(self, result, stdout='', stderr=''):
        contents = json.dumps(result) + stdout + stderr
        contents += ''.join(path.read_text() for path in self.state_dir.glob('*.json'))
        for value in (self.client.credentials.api_key, self.client.credentials.api_secret,
                      self.client.credentials.passphrase, 'Traceback'):
            self.assertNotIn(value, contents)

    def test_malformed_startup_position_row_halts_cleanly_without_writes(self):
        self.client.position_rows[MARKETS[0]] = [['test-secret']]
        with contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = self.run_bot()
        self.assertTrue(result['halted'])
        self.assertEqual(self.client.leverage_calls, [])
        self.assertEqual(self.client.place_calls, [])
        self.assert_safe_failure_output(result, stdout.getvalue(), stderr.getvalue())

    def test_unexpected_second_post_exception_preserves_all_intents_and_blocks_restart(self):
        def unexpected(inst_id):
            if inst_id == MARKETS[1]:
                raise RuntimeError('test-secret test-passphrase')
        self.client.after_place = unexpected
        with contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = self.run_bot()
        self.assertTrue(result['halted'])
        self.assertEqual(len(self.client.place_calls), 2)
        journal = self.journal()
        self.assertEqual({row['instrument'] for row in journal['orders']}, set(MARKETS[:2]))
        self.assertTrue(all(row['status'] == 'inspect_required' for row in journal['orders']))
        self.assertEqual(result['markets'][MARKETS[0]]['position']['side'], 'long')
        self.assertTrue(result['markets'][MARKETS[0]]['position_known'])
        self.assertFalse(result['markets'][MARKETS[1]]['position_known'])
        self.assertIn('unknown', result['markets'][MARKETS[1]]['position']['status'])
        self.assert_safe_failure_output(result, stdout.getvalue(), stderr.getvalue())
        self.client.after_place = None
        for market in MARKETS:
            self.client.position_rows[market] = []
            self.client.algo_rows[market] = []
        leverages = len(self.client.leverage_calls)
        again = self.run_bot()
        self.assertTrue(again['halted'])
        self.assertEqual(len(self.client.place_calls), 2)
        self.assertEqual(len(self.client.leverage_calls), leverages)
        self.assertEqual(self.journal(), journal)

    def test_malformed_fill_response_halts_after_one_submit_and_preserves_intent(self):
        queries = []
        def malformed(_inst_id, _client_id):
            queries.append(1)
            return []
        self.client.query_order = malformed
        with contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = self.run_bot()
        self.assertTrue(result['halted'])
        self.assertEqual(len(self.client.place_calls), 1)
        self.assertEqual(len(queries), 1)
        self.assertEqual(self.journal()['orders'][0]['status'], 'inspect_required')
        self.assertFalse(result['markets'][MARKETS[0]]['position_known'])
        self.assert_safe_failure_output(result, stdout.getvalue(), stderr.getvalue())

    def test_unknown_first_order_stops_other_symbols_and_blocks_restart(self):
        self.client.fail_ambiguous_market = MARKETS[0]
        result = self.run_bot()
        self.assertTrue(result['halted'])
        self.assertEqual(len(self.client.place_calls), 1)
        entry = self.journal()['orders'][-1]
        self.assertEqual(entry['instrument'], MARKETS[0])
        self.assertEqual(entry['status'], 'inspect_required')
        for market in MARKETS:
            self.client.position_rows[market] = []
            self.client.algo_rows[market] = []
        leverage_before = len(self.client.leverage_calls)
        again = self.run_bot()
        self.assertTrue(again['halted'])
        self.assertEqual(len(self.client.place_calls), 1)
        self.assertEqual(len(self.client.leverage_calls), leverage_before)

    def test_one_missing_protective_order_halts_whole_portfolio(self):
        self.client.hide_protection_market = MARKETS[0]
        result = self.run_bot()
        self.assertTrue(result['halted'])
        self.assertEqual(len(self.client.place_calls), 1)
        self.assertEqual(self.journal()['orders'][0]['status'], 'inspect_required')

    def test_global_drawdown_after_first_entry_stops_other_entries_without_flattening(self):
        def drawdown(_instrument):
            self.client.equity = D('4000')
        self.client.after_place = drawdown
        result = self.run_bot()
        self.assertTrue(result['halted'])
        self.assertEqual(len(self.client.place_calls), 1)
        self.assertTrue(self.client.position_rows[MARKETS[0]])
        self.assertEqual(self.client.place_calls[0]['side'], 'buy')

    def test_protected_eth_50001_read_recovers_without_replaying_entry(self):
        result, failures, snapshots, _original_algos = self.protected_eth_read_failure(fail_once=True)
        self.assertFalse(result['halted'], result)
        self.assertEqual(len(failures), 1)
        self.assertEqual(result['entries'], 1)
        self.assertEqual(len(self.client.place_calls), 1)
        self.assertEqual(self.client.place_calls[0]['instrument'], MARKETS[1])
        self.assertEqual(result['markets'][MARKETS[1]]['position']['side'], 'long')
        self.assertTrue(result['markets'][MARKETS[1]]['position_known'])
        self.assertTrue(self.client.position_rows[MARKETS[1]])
        self.assertEqual(self.journal()['orders'][0]['status'], 'protected')
        events = [state['connection']['state'] for state in snapshots]
        retry = events.index('retrying')
        checked = events.index('checking', retry + 1)
        self.assertIn('connected', events[checked + 1:])
        self.assertEqual(self.stop.waits.count(2), 1)
        self.assertNotIn(4, self.stop.waits)

    def test_protected_eth_50001_exhaustion_stops_without_replay_then_flat_terminal_reconciles(self):
        result, failures, _snapshots, original_algos = self.protected_eth_read_failure(fail_once=False)
        self.assertTrue(result['halted'])
        self.assertEqual(result['halt_api_code'], '50001')
        self.assertEqual(len(failures), 3)
        self.assertEqual(result['entries'], 1)
        self.assertEqual(len(self.client.place_calls), 1)
        identity = self.client.place_calls[0]['client_id']
        self.assertEqual(self.journal()['orders'][0]['client_id'], identity)
        self.assertEqual(self.journal()['orders'][0]['status'], 'inspect_required')
        self.assertTrue(self.client.position_rows[MARKETS[1]])
        self.assertEqual(self.stop.waits.count(2), 1)
        self.assertEqual(self.stop.waits.count(4), 1)

        # The user verifies/clears every market at the exchange. This does not
        # itself release the durable recovery block or cause an entry replay.
        for inst in MARKETS:
            self.client.position_rows[inst] = []
            self.client.order_rows[inst] = []
            self.client.algo_rows[inst] = []
        self.client.pending_algo_orders = original_algos
        leverage_before = len(self.client.leverage_calls)
        restarted = self.run_bot()
        self.assertTrue(restarted['halted'])
        self.assertIn('journal', restarted['halt_reason'])
        self.assertEqual(len(self.client.place_calls), 1)
        self.assertEqual(len(self.client.leverage_calls), leverage_before)

        self.client.calls.clear()
        recovered = portfolio.reconcile_portfolio(config(), self.state_dir, client=self.client)
        self.assertFalse(recovered['halted'], recovered)
        self.assertTrue(recovered['reconciled'])
        self.assertEqual(recovered['closed'], 1)
        self.assertEqual(self.journal()['orders'][0]['status'], 'closed')
        self.assertIn(('query_order', MARKETS[1]), self.client.calls)
        for inst in MARKETS:
            self.assertGreaterEqual(self.client.calls.count(('positions', inst)), 2)
            self.assertGreaterEqual(self.client.calls.count(('pending_orders', inst)), 2)
            self.assertGreaterEqual(self.client.calls.count(('pending_algo_orders', inst)), 2)
        self.assertEqual(len(self.client.place_calls), 1)
        self.assertEqual(len(self.client.leverage_calls), leverage_before)

    def test_flat_after_protected_eth_read_halt_is_not_enough_if_order_remains_unknown(self):
        _result, _failures, _snapshots, original_algos = self.protected_eth_read_failure(fail_once=False)
        original = self.journal()
        for inst in MARKETS:
            self.client.position_rows[inst] = []
            self.client.order_rows[inst] = []
            self.client.algo_rows[inst] = []
        self.client.pending_algo_orders = original_algos
        identity = self.client.place_calls[0]['client_id']
        self.client.orders[identity]['state'] = 'live'
        recovered = portfolio.reconcile_portfolio(config(), self.state_dir, client=self.client)
        self.assertTrue(recovered['halted'])
        self.assertFalse(recovered['reconciled'])
        self.assertEqual(self.journal(), original)
        self.assertEqual(len(self.client.place_calls), 1)

    def test_cycle_end_refresh_checks_drawdown_after_fourth_fill_without_flattening(self):
        def fourth_fill(_instrument):
            if len(self.client.place_calls) == 4:
                self.client.equity = D('4000')
                self.client.now = NOW + 5_000
        ordinary_balance = self.client.balances
        def delayed_final_balance():
            if len(self.client.place_calls) == 4:
                self.client.now += 700
            return ordinary_balance()
        self.client.after_place = fourth_fill
        self.client.balances = delayed_final_balance
        result = self.run_bot()
        self.assertEqual(len(self.client.place_calls), 4)
        self.assertEqual(result['entries'], 4)
        self.assertTrue(result['halted'])
        self.assertEqual(D(result['equity']), D(4000))
        self.assertEqual(result['equity_checked_at_ms'], NOW + 5_000)
        self.assertTrue(all(self.client.position_rows[market] for market in MARKETS))
        self.assertTrue(all(order['side'] == 'buy' for order in self.client.place_calls))

    def test_global_cap_uses_current_marks_of_held_positions(self):
        def basis_change(inst_id):
            if inst_id == MARKETS[0]:
                self.client.prices[inst_id] *= D('2.5')
        self.client.after_place = basis_change
        result = self.run_bot()
        self.assertLessEqual(self.client.current_notional(), D(800))
        self.assertGreater(self.client.current_notional(), D(self.client.place_calls[0]['notional']))
        self.assertLessEqual(len(self.client.place_calls), 4)
        self.assertTrue(self.client.position_rows[MARKETS[0]])
        # A normal run above proves all four can open; a price jump must reduce
        # later quantities or stop further entries rather than reuse old marks.
        if len(self.client.place_calls) >= 3:
            self.assertLess(self.client.place_calls[2]['notional'], D(200))

    def test_stale_marks_never_open_positions(self):
        self.client.stale_marks = True
        result = self.run_bot()
        self.assertEqual(self.client.place_calls, [])
        self.assertEqual(result['entries'], 0)

    def age_signals_after_preflight(self, age_ms):
        ordinary_leverage = self.client.get_leverage
        def advance_after_preflight(inst_id):
            leverage = ordinary_leverage(inst_id)
            if inst_id == MARKETS[-1]:
                self.client.now = NOW + age_ms
            return leverage
        self.client.get_leverage = advance_after_preflight

    def test_121_second_old_signal_is_skipped_despite_fresh_private_inputs(self):
        self.age_signals_after_preflight(121_000)
        result = self.run_bot()
        self.assertFalse(result['halted'], result)
        self.assertEqual(self.client.place_calls, [])
        self.assertEqual(result['entries'], 0)
        self.assertEqual(result['skipped_signals'], 4)

    def test_119_second_old_signal_can_enter_when_all_private_inputs_are_fresh(self):
        self.age_signals_after_preflight(119_000)
        result = self.run_bot()
        self.assertFalse(result['halted'], result)
        self.assertEqual(result['entries'], 4)
        self.assertEqual(len(self.client.place_calls), 4)

    def test_held_mark_timestamps_are_rechecked_after_later_market_reads(self):
        ordinary_mark = self.client.mark_price
        def aging_mark(inst_id):
            price, timestamp = ordinary_mark(inst_id)
            if self.client.position_rows[MARKETS[0]]:
                if inst_id == MARKETS[0]:
                    return price, self.client.now - 29_000
                self.client.now += 2_000
                return price, self.client.now
            return price, timestamp
        self.client.mark_price = aging_mark
        result = self.run_bot()
        self.assertEqual(len(self.client.place_calls), 1)
        self.assertEqual(self.client.place_calls[0]['instrument'], MARKETS[0])
        self.assertEqual(result['entries'], 1)

    def test_held_crossings_do_not_reverse_or_pyramid(self):
        class NextBarShort:
            def __init__(self, fast, slow):
                pass
            def update(self, bar):
                return 'short' if bar.ts >= NOW else 'long'
        self.stop.on_wait = lambda seconds: setattr(self.client, 'now', self.client.now + INTERVAL) if seconds == .001 else None
        with patch('okx_bot.portfolio.CrossStrategy', NextBarShort):
            result = self.run_bot(cycles=2)
        self.assertFalse(result['halted'], result)
        self.assertEqual(len(self.client.place_calls), 4)
        self.assertTrue(all(row['side'] == 'buy' for row in self.client.place_calls))
        self.assertTrue(all(market['position']['side'] == 'long' for market in result['markets'].values()))

    def test_candidate_priority_rotates_by_signal_batch_instead_of_poll_cycle(self):
        polls = []
        def next_poll(seconds):
            if seconds != .001:
                return
            polls.append(seconds)
            if len(polls) == 1:
                # Simulate native TP/SL closing the first batch before a poll
                # with no new closed bar. This poll must not rotate priority.
                for market in MARKETS:
                    self.client.position_rows[market] = []
                    self.client.algo_rows[market] = []
            elif len(polls) == 2:
                self.client.now += INTERVAL
        self.stop.on_wait = next_poll
        result = self.run_bot(cycles=3)
        self.assertFalse(result['halted'], result)
        ordered = [order['instrument'] for order in self.client.place_calls]
        self.assertEqual(ordered[:4], list(MARKETS))
        self.assertEqual(ordered[4:], list(MARKETS[1:] + MARKETS[:1]))
        self.assertEqual(result['entries'], 8)
        self.assertEqual(len(polls), 2)

    def test_new_bar_without_any_crossing_does_not_rotate_candidate_priority(self):
        class NoMiddleCrossing:
            def __init__(self, fast, slow):
                pass
            def update(self, bar):
                return None if bar.ts == NOW else 'long'
        polls = []
        def next_bar(seconds):
            if seconds != .001:
                return
            polls.append(seconds)
            if len(polls) == 1:
                for market in MARKETS:
                    self.client.position_rows[market] = []
                    self.client.algo_rows[market] = []
            self.client.now += INTERVAL
        self.stop.on_wait = next_bar
        with patch('okx_bot.portfolio.CrossStrategy', NoMiddleCrossing):
            result = self.run_bot(cycles=3)
        self.assertFalse(result['halted'], result)
        ordered = [order['instrument'] for order in self.client.place_calls]
        self.assertEqual(ordered[:4], list(MARKETS))
        self.assertEqual(ordered[4:], list(MARKETS[1:] + MARKETS[:1]))
        self.assertEqual(result['entries'], 8)

    def test_single_key_lock_is_shared_with_original_demo_runner(self):
        fingerprint = hashlib.sha256(self.client.credentials.api_key.encode()).hexdigest()
        path = self.state_dir / 'status.json'
        sentinel = '{"mode":"demo","scope":"portfolio","phase":"running","sentinel":"existing worker"}'
        path.write_text(sentinel)
        with demo._lock(Path('/tmp') / ('okx-demo-' + fingerprint + '.lock')):
            result = self.run_bot()
        self.assertTrue(result['halted'])
        self.assertEqual(self.client.place_calls, [])
        self.assertEqual(self.client.leverage_calls, [])
        self.assertEqual(path.read_text(), sentinel)

    def test_starting_and_preflight_progress_exist_before_account_or_order_requests(self):
        self.client.add_new = False
        snapshots = []
        ordinary_save = portfolio._save
        def observe(path, contents):
            ordinary_save(path, contents)
            if path.name == 'status.json':
                snapshots.append((json.loads(json.dumps(contents)), list(self.client.calls)))
        with patch('okx_bot.portfolio._save', side_effect=observe):
            result = self.run_bot()
        self.assertFalse(result['halted'], result)
        first, calls = snapshots[0]
        self.assertEqual(first['scope'], 'portfolio')
        self.assertEqual(first['phase'], 'starting')
        self.assertEqual(calls, [])
        self.assertIsNone(first.get('last_success_at_ms'))
        self.assertTrue(all(not market['position_known'] for market in first['markets'].values()))
        self.assertTrue(any(all(('positions', market) in calls for market in MARKETS)
                            and not any(method == 'set_leverage' for method, _market in calls)
                            for _snapshot, calls in snapshots))
        for market in MARKETS:
            self.assertTrue(any(snapshot['markets'][market].get('leverage_verified') is True
                                and ('get_leverage', market) in calls for snapshot, calls in snapshots))

    def test_legacy_unresolved_journal_is_preserved_and_blocks_portfolio(self):
        original = {'version': 1, 'orders': [{'instrument': MARKETS[0], 'status': 'intent', 'client_id': 'oldorder'}]}
        path = self.state_dir / 'demo-journal.json'
        path.write_text(json.dumps(original))
        result = self.run_bot()
        self.assertTrue(result['halted'])
        self.assertEqual(json.loads(path.read_text()), original)
        self.assertEqual(self.client.place_calls, [])
        self.assertEqual(self.client.leverage_calls, [])

    def test_leverage_write_failure_is_never_retried(self):
        self.client.fail_leverage = True
        result = self.run_bot()
        self.assertTrue(result['halted'])
        self.assertEqual(len(self.client.leverage_calls), 1)
        self.assertEqual(self.client.place_calls, [])
        self.assertEqual(self.stop.waits, [])

    def test_stop_at_start_is_quiet_and_has_no_mutations(self):
        self.stop.set()
        result = self.run_bot()
        self.assertFalse(result['halted'], result)
        self.assertEqual(self.client.leverage_calls, [])
        self.assertEqual(self.client.place_calls, [])

    def test_status_and_journal_contain_no_credential_values(self):
        self.run_bot()
        serialized = ''.join(path.read_text() for path in self.state_dir.glob('*.json'))
        for value in (self.client.credentials.api_key, self.client.credentials.api_secret, self.client.credentials.passphrase):
            self.assertNotIn(value, serialized)

    def unresolved_journal(self, markets=(MARKETS[0], MARKETS[1])):
        journal = {'version': 1, 'orders': []}
        for index, market in enumerate(markets):
            client_id = 'oldorder' + str(index)
            journal['orders'].append({'instrument': market, 'client_id': client_id,
                                      'status': 'protected' if index == 0 else 'inspect_required'})
            self.client.orders[client_id] = {'instId': market, 'clOrdId': client_id,
                                            'state': 'filled', 'accFillSz': '1'}
        (self.state_dir / 'demo-journal.json').write_text(json.dumps(journal))
        return journal

    def assert_no_exchange_writes(self):
        self.assertEqual(self.client.place_calls, [])
        self.assertEqual(self.client.leverage_calls, [])

    def test_reconcile_mixed_market_journal_only_after_all_four_markets_are_flat(self):
        self.unresolved_journal()
        result = portfolio.reconcile_portfolio(config(), self.state_dir, client=self.client)
        self.assertFalse(result['halted'], result)
        self.assertTrue(result['reconciled'])
        self.assertEqual(result['closed'], 2)
        self.assertTrue(all(row['status'] == 'closed' for row in self.journal()['orders']))
        for market in MARKETS:
            self.assertGreaterEqual(self.client.calls.count(('positions', market)), 2)
            self.assertGreaterEqual(self.client.calls.count(('pending_orders', market)), 2)
            self.assertGreaterEqual(self.client.calls.count(('pending_algo_orders', market)), 2)
        self.assert_no_exchange_writes()

    def test_reconcile_legacy_single_market_journal_also_checks_other_three_legs(self):
        self.unresolved_journal(markets=(MARKETS[0],))
        result = portfolio.reconcile_portfolio(config(), self.state_dir, client=self.client)
        self.assertFalse(result['halted'], result)
        self.assertEqual(result['closed'], 1)
        self.assertEqual(self.journal()['orders'][0]['status'], 'closed')
        for market in MARKETS:
            self.assertGreaterEqual(self.client.calls.count(('positions', market)), 2)
        self.assert_no_exchange_writes()

    def test_reconcile_any_leg_position_or_pending_order_preserves_entire_journal(self):
        original = self.unresolved_journal()
        fixtures = (
            (self.client.position_rows, MARKETS[-1], [{'instId': MARKETS[-1], 'pos': '1', 'avgPx': '.1'}]),
            (self.client.order_rows, MARKETS[1], [{'external': True}]),
            (self.client.algo_rows, MARKETS[2], [{'external': True}]),
        )
        for container, market, rows in fixtures:
            with self.subTest(market=market, rows=rows):
                container[market] = rows
                result = portfolio.reconcile_portfolio(config(), self.state_dir, client=self.client)
                self.assertTrue(result['halted'])
                self.assertFalse(result['reconciled'])
                self.assertEqual(self.journal(), original)
                container[market] = []
        self.assert_no_exchange_writes()

    def test_reconcile_unknown_or_partial_order_cannot_partly_clear_mixed_journal(self):
        original = self.unresolved_journal()
        first_order = self.client.orders['oldorder1']
        for bad_order in (
                {**first_order, 'state': 'partially_filled'},
                {**first_order, 'clOrdId': 'differentid'},
                {**first_order, 'instId': MARKETS[0]}):
            with self.subTest(order=bad_order):
                self.client.orders['oldorder1'] = bad_order
                result = portfolio.reconcile_portfolio(config(), self.state_dir, client=self.client)
                self.assertTrue(result['halted'])
                self.assertEqual(self.journal(), original)
        def unavailable(_market, _client_id):
            raise OkxError('OKX rejected request (code 51603)', api_code='51603', category='api')
        self.client.query_order = unavailable
        result = portfolio.reconcile_portfolio(config(), self.state_dir, client=self.client)
        self.assertTrue(result['halted'])
        self.assertEqual(self.journal(), original)
        self.assert_no_exchange_writes()

    def test_reconcile_malformed_query_is_safely_rejected_without_journal_changes(self):
        original = self.unresolved_journal()
        self.client.query_order = lambda _inst_id, _client_id: []
        with contextlib.redirect_stdout(io.StringIO()) as stdout, contextlib.redirect_stderr(io.StringIO()) as stderr:
            result = portfolio.reconcile_portfolio(config(), self.state_dir, client=self.client)
        self.assertTrue(result['halted'])
        self.assertFalse(result['reconciled'])
        self.assertEqual(self.journal(), original)
        self.assert_no_exchange_writes()
        self.assert_safe_failure_output(result, stdout.getvalue(), stderr.getvalue())

    def test_reconcile_unknown_instrument_remains_blocked(self):
        original = self.unresolved_journal(markets=('DOGE-USDT-SWAP',))
        result = portfolio.reconcile_portfolio(config(), self.state_dir, client=self.client)
        self.assertTrue(result['halted'])
        self.assertEqual(self.journal(), original)
        self.assert_no_exchange_writes()


if __name__ == '__main__':
    unittest.main()
