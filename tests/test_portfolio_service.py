"""Long-lived portfolio recovery and restart adoption, with fake exchange IO."""
import copy
import hashlib
import json
import tempfile
import unittest
from decimal import Decimal as D
from pathlib import Path
from unittest.mock import patch

from okx_bot import demo, portfolio
from okx_bot.okx import AmbiguousOrderError, Credentials, OkxError
from okx_bot.session import PortfolioSession, profile_fingerprint

from test_portfolio import (AlwaysLong, FakePortfolioClient, INTERVAL, MARKETS,
                            NOW, NoSleepStop, config)


class ServiceExchange(FakePortfolioClient):
    """Strict bulk-SWAP endpoints used by the service's snapshot facade."""
    def swap_positions(self):
        self.calls.append(('swap_positions', None))
        return copy.deepcopy([row for market in MARKETS for row in self.position_rows[market]])

    def swap_pending_orders(self):
        self.calls.append(('swap_pending_orders', None))
        return copy.deepcopy([row for market in MARKETS for row in self.order_rows[market]])

    def swap_pending_algos(self, order_type):
        self.calls.append(('swap_pending_algos', order_type))
        return copy.deepcopy([row for market in MARKETS for row in self.algo_rows[market]
                              if row.get('ordType') == order_type])

    def swap_mark_prices(self):
        self.calls.append(('swap_mark_prices', None))
        return [{'instId': market, 'markPx': str(self.prices[market]), 'ts': str(self.now)} for market in MARKETS]

    def place_market_order(self, inst_id, side, size, client_id, *, stop_loss, take_profit):
        result = super().place_market_order(inst_id, side, size, client_id,
                                           stop_loss=stop_loss, take_profit=take_profit)
        self.orders[client_id]['avgPx'] = self.position_rows[inst_id][0]['avgPx']
        for row in self.algo_rows[inst_id]:
            row.update(algoId=str(1000 + len(self.orders)), ordType='oco')
        return result


class ServicePortfolioTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state_dir = Path(self.temporary.name)
        self.client = ServiceExchange(self.state_dir)
        self.assertTrue(all(not self.client.position_rows[market]
                            and not self.client.order_rows[market]
                            and not self.client.algo_rows[market] for market in MARKETS))
        PortfolioSession.load_or_create(self.state_dir,
            hashlib.sha256(self.client.credentials.api_key.encode()).hexdigest(),
            profile_fingerprint(config()), D(5000), allow_initialize=True)
        self.stop = NoSleepStop()
        for target in ('okx_bot.portfolio._now_ms', 'okx_bot.demo._now_ms', 'okx_bot.snapshot._now_ms'):
            clock = patch(target, side_effect=lambda: self.client.now)
            clock.start()
            self.addCleanup(clock.stop)
        strategy = patch('okx_bot.portfolio.CrossStrategy', AlwaysLong)
        strategy.start()
        self.addCleanup(strategy.stop)

    def run_bot(self, *, cycles=1, profile=None, resilient=True, resume=True):
        return portfolio.run_portfolio(profile or config(), self.state_dir, self.stop,
            poll_seconds=.001, client=self.client, max_cycles=cycles,
            resilient=resilient, resume=resume)

    def journal(self):
        return json.loads((self.state_dir / 'demo-journal.json').read_text())

    def session(self):
        return json.loads((self.state_dir / 'session.json').read_text())

    def seed_protected(self):
        previous_wait = self.stop.on_wait
        self.stop.on_wait = self.advance_bar_on_poll
        try:
            result = self.run_bot(cycles=2)
        finally:
            self.stop.on_wait = previous_wait
        self.assertFalse(result['halted'], result)
        self.assertEqual(result['entries'], 4)
        self.assertTrue(all(row['status'] == 'protected' for row in self.journal()['orders']))
        self.client.place_calls.clear()
        self.client.leverage_calls.clear()
        self.client.calls.clear()
        self.stop.waits.clear()
        return result

    def advance_bar_on_poll(self, seconds):
        if seconds == .001:
            self.client.now += INTERVAL

    def assert_no_posts(self):
        self.assertEqual(self.client.place_calls, [])
        self.assertEqual(self.client.leverage_calls, [])

    def collect_snapshots(self, invoke):
        snapshots = []
        ordinary_save = portfolio._save
        def observe(path, value):
            ordinary_save(path, value)
            if path.name == 'status.json':
                journal_path = self.state_dir / 'demo-journal.json'
                statuses = [row['status'] for row in self.journal()['orders']] if journal_path.exists() else []
                snapshots.append({'state': copy.deepcopy(value), 'calls': list(self.client.calls),
                                  'posts': len(self.client.place_calls), 'statuses': statuses})
        with patch('okx_bot.portfolio._save', side_effect=observe):
            result = invoke()
        return result, snapshots

    def test_resume_adopts_only_verified_protected_orders_without_any_post(self):
        original = self.seed_protected()
        result = self.run_bot()
        self.assertFalse(result['halted'], result)
        self.assert_no_posts()
        self.assertTrue(all(row['status'] == 'protected' for row in self.journal()['orders']))
        self.assertEqual(set(result['markets']), set(MARKETS))
        for market in MARKETS:
            self.assertEqual(result['markets'][market]['position']['side'], 'long')
            self.assertTrue(result['markets'][market]['position_known'])
            self.assertEqual(result['markets'][market]['position']['contracts'],
                             original['markets'][market]['position']['contracts'])

    def test_default_non_resume_mode_still_blocks_an_unresolved_journal(self):
        self.seed_protected()
        result = self.run_bot(resilient=False, resume=False)
        self.assertTrue(result['halted'])
        self.assert_no_posts()

    def test_resume_rejects_wrong_original_order_id_without_submitting(self):
        self.seed_protected()
        identity = self.journal()['orders'][0]['client_id']
        self.client.orders[identity]['ordId'] = 'not-original-order'
        result = self.run_bot()
        self.assertTrue(result['halted'])
        self.assert_no_posts()

    def test_resume_rejects_wrong_client_identity_or_filled_size(self):
        self.seed_protected()
        journal = self.journal()
        identity = journal['orders'][0]['client_id']
        original = dict(self.client.orders[identity])
        for changes in ({'clOrdId': 'differentid'}, {'accFillSz': str(D(original['accFillSz']) + 1)}):
            with self.subTest(changes=changes):
                (self.state_dir / 'demo-journal.json').write_text(json.dumps(journal))
                self.client.orders[identity] = {**original, **changes}
                result = self.run_bot()
                self.assertTrue(result['halted'])
                self.assert_no_posts()

    def test_resume_rejects_wrong_missing_or_nonfinite_original_average_price(self):
        self.seed_protected()
        journal = self.journal()
        identity = journal['orders'][0]['client_id']
        original = dict(self.client.orders[identity])
        for value in ('101', None, 'NaN', '0', '-1'):
            with self.subTest(avgPx=value):
                (self.state_dir / 'demo-journal.json').write_text(json.dumps(journal))
                self.client.orders[identity] = dict(original)
                if value is None:
                    self.client.orders[identity].pop('avgPx')
                else:
                    self.client.orders[identity]['avgPx'] = value
                result = self.run_bot()
                self.assertTrue(result['halted'])
                self.assert_no_posts()

    def test_resume_rejects_position_or_protection_mismatch(self):
        self.seed_protected()
        journal = self.journal()
        original_pos = copy.deepcopy(self.client.position_rows[MARKETS[0]])
        original_algo = copy.deepcopy(self.client.algo_rows[MARKETS[0]])
        for kind in ('size', 'leverage', 'average_price', 'bracket'):
            with self.subTest(kind=kind):
                (self.state_dir / 'demo-journal.json').write_text(json.dumps(journal))
                self.client.position_rows[MARKETS[0]] = copy.deepcopy(original_pos)
                self.client.algo_rows[MARKETS[0]] = copy.deepcopy(original_algo)
                if kind == 'size':
                    self.client.position_rows[MARKETS[0]][0]['pos'] = str(D(original_pos[0]['pos']) + 1)
                elif kind == 'leverage':
                    self.client.position_rows[MARKETS[0]][0]['lever'] = '2'
                elif kind == 'average_price':
                    self.client.position_rows[MARKETS[0]][0]['avgPx'] = str(D(original_pos[0]['avgPx']) + 1)
                else:
                    self.client.algo_rows[MARKETS[0]][0]['algoClOrdId'] = 'different-protective-id'
                result = self.run_bot()
                self.assertTrue(result['halted'])
                self.assert_no_posts()

    def test_unsafe_journal_states_remain_quarantined_even_if_exchange_appears_protected(self):
        self.seed_protected()
        original = self.journal()
        for status in ('intent', 'accepted', 'inspect_required'):
            with self.subTest(status=status):
                journal = copy.deepcopy(original)
                journal['orders'][0]['status'] = status
                (self.state_dir / 'demo-journal.json').write_text(json.dumps(journal))
                result = self.run_bot()
                self.assertTrue(result['halted'])
                self.assert_no_posts()

    def test_resume_closes_terminal_original_records_only_when_all_markets_flat(self):
        self.seed_protected()
        for market in MARKETS:
            self.client.position_rows[market] = []
            self.client.algo_rows[market] = []
        result = self.run_bot()
        self.assertFalse(result['halted'], result)
        self.assert_no_posts()
        self.assertTrue(all(row['status'] == 'closed' for row in self.journal()['orders']))
        self.assertTrue(all(state['position'] is None for state in result['markets'].values()))

    def test_resume_missing_original_order_cannot_adopt_positions(self):
        self.seed_protected()
        def missing(_instrument, _client_id):
            raise OkxError('Original order unavailable', api_code='51603', category='api')
        self.client.query_order = missing
        result = self.run_bot()
        self.assertTrue(result['halted'])
        self.assert_no_posts()
        self.assertTrue(any(row['status'] != 'closed' for row in self.journal()['orders']))

    def test_session_binding_rejects_a_different_api_key(self):
        self.seed_protected()
        self.client.credentials = Credentials('another-key', 'test-secret', 'test-passphrase')
        result = self.run_bot()
        self.assertTrue(result['halted'])
        self.assert_no_posts()

    def test_session_binding_rejects_different_strategy_configuration(self):
        self.seed_protected()
        result = self.run_bot(profile=config(max_notional=D(100)))
        self.assertTrue(result['halted'])
        self.assert_no_posts()

    def test_active_positions_cannot_resume_after_deleting_the_risk_state(self):
        self.seed_protected()
        (self.state_dir / 'session.json').unlink()
        result = self.run_bot()
        self.assertTrue(result['halted'])
        self.assert_no_posts()
        self.assertFalse((self.state_dir / 'session.json').exists())

    def test_existing_session_with_missing_journal_blocks_without_creating_empty_state(self):
        self.seed_protected()
        original_session = (self.state_dir / 'session.json').read_bytes()
        (self.state_dir / 'demo-journal.json').unlink()
        result = self.run_bot()
        self.assertTrue(result['halted'])
        self.assert_no_posts()
        self.assertFalse((self.state_dir / 'demo-journal.json').exists())
        self.assertEqual((self.state_dir / 'session.json').read_bytes(), original_session)

    def test_risk_peak_and_latched_drawdown_survive_restarts_and_flat_cleanup(self):
        self.seed_protected()
        self.assertEqual(D(self.session()['peak_equity']), D(5000))
        self.client.equity = D(4000)
        stopped = self.run_bot()
        self.assertTrue(stopped['halted'])
        self.assertTrue(self.session()['risk_halted'])
        self.assertEqual(D(self.session()['peak_equity']), D(5000))
        for market in MARKETS:
            self.client.position_rows[market] = []
            self.client.algo_rows[market] = []
        cleanup = portfolio.reconcile_portfolio(config(), self.state_dir, client=self.client)
        self.assertTrue(cleanup['reconciled'], cleanup)
        self.assertTrue(all(row['status'] == 'closed' for row in self.journal()['orders']))
        self.client.equity = D(5000)
        again = self.run_bot()
        self.assertTrue(again['halted'])
        self.assertTrue(self.session()['risk_halted'])
        self.assert_no_posts()

    def test_startup_read_exhaustion_recovers_without_releasing_the_key_lock(self):
        self.client.add_new = False
        original = self.client.account_config
        reads = []
        locked = []
        failed = [True]
        def unavailable():
            reads.append(1)
            if failed[0]:
                raise OkxError('fake-secret', category='transport')
            return original()
        self.client.account_config = unavailable
        def recovery_wait(seconds):
            if seconds >= 15:
                fingerprint = hashlib.sha256(self.client.credentials.api_key.encode()).hexdigest()
                try:
                    with demo._lock(Path('/tmp') / ('okx-demo-' + fingerprint + '.lock')):
                        locked.append(False)
                except demo.DemoHalt:
                    locked.append(True)
                failed[0] = False
            if len(self.stop.waits) > 30:
                self.stop.set()
        self.stop.on_wait = recovery_wait
        result, snapshots = self.collect_snapshots(lambda: self.run_bot())
        self.assertFalse(result['halted'], result)
        self.assertEqual(reads[:3], [1, 1, 1])
        self.assertGreaterEqual(len(reads), 4)
        self.assertEqual(locked, [True])
        self.assertEqual(self.client.place_calls, [])
        self.assertTrue(any(row['state']['phase'] == 'recovering' for row in snapshots))
        self.assertTrue(any(row['state']['connection']['state'] == 'connected' for row in snapshots))

    def test_startup_leverage_get_recovers_without_any_leverage_post(self):
        self.client.add_new = False
        original = self.client.get_leverage
        failed = [True]
        def unavailable(inst_id):
            if inst_id == MARKETS[0] and failed[0]:
                raise OkxError('temporary read error', category='timeout')
            return original(inst_id)
        self.client.get_leverage = unavailable
        self.stop.on_wait = lambda seconds: failed.__setitem__(0, False) if seconds >= 15 else None
        result = self.run_bot(resume=False)
        self.assertFalse(result['halted'], result)
        self.assertEqual(self.client.leverage_calls, [])
        for market in MARKETS:
            self.assertIn(('get_leverage', market), self.client.calls)
        self.assertEqual(self.client.place_calls, [])

    def test_outer_recovery_backoff_continues_past_ten_minutes_until_stop(self):
        self.client.add_new = False
        self.client.account_config = lambda: (_ for _ in ()).throw(OkxError('temporary', category='transport'))
        outer_delays = []
        def long_outage(seconds):
            if seconds >= 15:
                outer_delays.append(seconds)
                self.client.now += int(seconds * 1000)
                if sum(outer_delays) > 660:
                    self.stop.set()
            if len(self.stop.waits) > 100:
                self.stop.set()
        self.stop.on_wait = long_outage
        with patch('okx_bot.portfolio.time.monotonic', side_effect=lambda: self.client.now / 1000):
            result, snapshots = self.collect_snapshots(lambda: self.run_bot())
        self.assertFalse(result['halted'], result)
        self.assertGreater(sum(outer_delays), 600)
        self.assertEqual(outer_delays[:3], [15, 30, 60])
        self.assertTrue(all(delay == 60 for delay in outer_delays[2:]))
        self.assertTrue(any(row['state']['phase'] == 'recovering' for row in snapshots))
        self.assertTrue(any(row['state'].get('recovery', {}).get('warning') is True for row in snapshots))
        self.assert_no_posts()

    def test_recovery_keeps_protected_journal_checks_all_markets_and_skips_outage_crossings(self):
        outage = [False]
        all_signals = [False]
        stage = [0]
        resumed_before_orders = []
        protected_during_outage = []
        original_balance = self.client.balances
        def balances():
            if outage[0]:
                raise OkxError('temporary account read failure', category='transport')
            return original_balance()
        self.client.balances = balances
        class EthThenAll:
            def __init__(self, fast, slow):
                pass
            def update(self, bar):
                # First batch holds ETH only. Crossings in all other markets
                # occur during the outage and must be swallowed by reseeding.
                return 'long' if all_signals[0] or D(9) < bar.close < D(11) else None
        def progress(seconds):
            if seconds == .001 and stage[0] == 0:
                self.assertEqual(self.client.place_calls, [])
                self.client.now += INTERVAL
                stage[0] = 1
            elif seconds == .001 and stage[0] == 1:
                self.assertEqual(len(self.client.place_calls), 1)
                self.assertEqual(self.client.place_calls[0]['instrument'], MARKETS[1])
                outage[0] = True
                all_signals[0] = True
                stage[0] = 2
            elif seconds >= 15 and stage[0] == 2:
                protected_during_outage.append(all(row['status'] == 'protected' for row in self.journal()['orders']))
                self.client.now += 2 * INTERVAL
                outage[0] = False
                self.client.calls.clear()
                stage[0] = 3
            elif seconds == .001 and stage[0] == 3:
                resumed_before_orders.append(len(self.client.place_calls))
                self.client.now += INTERVAL
                stage[0] = 4
            if len(self.stop.waits) > 50:
                self.stop.set()
        self.stop.on_wait = progress
        with patch('okx_bot.portfolio.CrossStrategy', EthThenAll):
            result, snapshots = self.collect_snapshots(lambda: self.run_bot(cycles=4))
        self.assertFalse(result['halted'], result)
        self.assertEqual(protected_during_outage, [True])
        self.assertEqual(resumed_before_orders, [1])
        self.assertEqual(len(self.client.place_calls), 4)
        self.assertEqual({row['instrument'] for row in self.client.place_calls}, set(MARKETS))
        recovering = [index for index, row in enumerate(snapshots) if row['state']['phase'] == 'recovering']
        self.assertTrue(recovering)
        self.assertTrue(all(row['posts'] == 1 for row in snapshots if row['state']['phase'] == 'recovering'))
        healthy = next(row for row in snapshots[recovering[-1] + 1:]
                       if row['state']['connection']['state'] == 'connected')
        self.assertEqual(healthy['posts'], 1)
        self.assertEqual(healthy['state']['recovery_checked_markets'], 4)
        for market in MARKETS:
            self.assertIn(('candles', market), healthy['calls'])
        for method in ('swap_positions', 'swap_pending_orders'):
            self.assertIn((method, None), healthy['calls'])
        for family in ('conditional', 'oco', 'trigger', 'move_order_stop'):
            self.assertIn(('swap_pending_algos', family), healthy['calls'])

    def test_recovery_preserves_previous_equity_peak_and_triggers_shared_drawdown(self):
        self.client.add_new = False
        self.client.equity = D(6000)
        outage = [False]
        first_cycle = [True]
        ordinary_balance = self.client.balances
        def balances():
            if outage[0]:
                raise OkxError('temporary read failure', category='transport')
            return ordinary_balance()
        self.client.balances = balances
        def progress(seconds):
            if seconds == .001 and first_cycle[0]:
                first_cycle[0] = False
                outage[0] = True
                self.client.equity = D(5000)
            elif seconds >= 15:
                outage[0] = False
            if len(self.stop.waits) > 30:
                self.stop.set()
        self.stop.on_wait = progress
        result = self.run_bot(cycles=3)
        self.assertTrue(result['halted'])
        self.assertEqual(D(result['peak_equity']), D(6000))
        self.assertEqual(D(self.session()['peak_equity']), D(6000))
        self.assertTrue(self.session()['risk_halted'])
        self.assertEqual(self.client.place_calls, [])

    def test_delayed_candles_do_not_retimestamp_cached_position_or_protection_facts(self):
        self.seed_protected()
        delayed = [False]
        source_start = []
        ordinary_candles = self.client.candles
        def candles(inst_id, bar, limit):
            if inst_id == MARKETS[-1] and delayed[0]:
                delayed[0] = False
                self.client.now += 31_000
            return ordinary_candles(inst_id, bar, limit)
        self.client.candles = candles
        def next_cycle(seconds):
            if seconds == .001:
                source_start.append(self.client.now)
                self.client.calls.clear()
                delayed[0] = True
        self.stop.on_wait = next_cycle
        result = self.run_bot(cycles=2)
        self.assertFalse(result['halted'], result)
        self.assertEqual(result['connection']['state'], 'connected')
        self.assert_no_posts()
        self.assertEqual(len(source_start), 1)
        self.assertEqual(result['last_success_at_ms'] - source_start[0], 31_000)
        for market in MARKETS:
            state = result['markets'][market]
            self.assertEqual(state['last_success_at_ms'], source_start[0])
            self.assertEqual(state['protection_checked_at_ms'], source_start[0])
            self.assertGreaterEqual(result['last_success_at_ms'] - state['last_success_at_ms'], 31_000)
        self.assertEqual(self.client.calls.count(('swap_positions', None)), 1)
        self.assertEqual(self.client.calls.count(('swap_pending_orders', None)), 1)
        for family in ('conditional', 'oco', 'trigger', 'move_order_stop'):
            self.assertEqual(self.client.calls.count(('swap_pending_algos', family)), 1)

    def test_recovery_does_not_partly_close_journal_before_all_markets_verify(self):
        outage = [False]
        later_market_failed = [False]
        stage = [0]
        before = []
        uncommitted = []
        ordinary_balance = self.client.balances
        ordinary_candles = self.client.candles
        def balances():
            if outage[0]:
                raise OkxError('temporary account read failure', category='transport')
            return ordinary_balance()
        def candles(inst_id, bar, limit):
            if inst_id == MARKETS[-1] and later_market_failed[0]:
                raise OkxError('temporary later-market read failure', category='timeout')
            return ordinary_candles(inst_id, bar, limit)
        self.client.balances = balances
        self.client.candles = candles
        class OnlyEth:
            def __init__(self, fast, slow):
                pass
            def update(self, bar):
                return 'long' if D(9) < bar.close < D(11) else None
        def progress(seconds):
            if seconds == .001 and stage[0] == 0:
                self.assertEqual(self.client.place_calls, [])
                self.client.now += INTERVAL
                stage[0] = 1
            elif seconds == .001 and stage[0] == 1:
                before.append((self.state_dir / 'demo-journal.json').read_bytes())
                outage[0] = True
                stage[0] = 2
            elif seconds >= 15 and stage[0] == 2:
                # ETH's native protection closed it during the outage. Its
                # local record must remain protected until the other legs pass.
                self.client.position_rows[MARKETS[1]] = []
                self.client.algo_rows[MARKETS[1]] = []
                self.client.now += 2 * INTERVAL
                outage[0] = False
                later_market_failed[0] = True
                stage[0] = 3
            elif seconds >= 15 and stage[0] == 3:
                uncommitted.append((self.state_dir / 'demo-journal.json').read_bytes())
                self.assertEqual(self.journal()['orders'][0]['status'], 'protected')
                later_market_failed[0] = False
                stage[0] = 4
            if len(self.stop.waits) > 50:
                self.stop.set()
        self.stop.on_wait = progress
        with patch('okx_bot.portfolio.CrossStrategy', OnlyEth):
            result = self.run_bot(cycles=3)
        self.assertFalse(result['halted'], result)
        self.assertEqual(uncommitted, before)
        self.assertEqual(len(self.client.place_calls), 1)
        self.assertEqual(self.client.place_calls[0]['instrument'], MARKETS[1])
        self.assertEqual(self.journal()['orders'][0]['status'], 'closed')
        self.assertTrue(all(state['position'] is None for state in result['markets'].values()))

    def test_stop_during_recovery_preserves_protected_bytes_and_holds_locks_until_return(self):
        outage = [False]
        stage = [0]
        before = []
        held = []
        ordinary_balance = self.client.balances
        fingerprint = hashlib.sha256(self.client.credentials.api_key.encode()).hexdigest()
        key_path = Path('/tmp') / ('okx-demo-' + fingerprint + '.lock')
        state_path = self.state_dir / 'demo.lock'
        def balances():
            if outage[0]:
                raise OkxError('temporary account read failure', category='transport')
            return ordinary_balance()
        self.client.balances = balances
        def progress(seconds):
            if seconds == .001 and stage[0] == 0:
                self.assertEqual(self.client.place_calls, [])
                self.client.now += INTERVAL
                stage[0] = 1
            elif seconds == .001 and stage[0] == 1:
                before.append((self.state_dir / 'demo-journal.json').read_bytes())
                outage[0] = True
                stage[0] = 2
            elif seconds >= 15:
                for path in (key_path, state_path):
                    try:
                        with demo._lock(path):
                            held.append(False)
                    except demo.DemoHalt:
                        held.append(True)
                self.stop.set()
            if len(self.stop.waits) > 30:
                self.stop.set()
        self.stop.on_wait = progress
        result = self.run_bot(cycles=3)
        self.assertFalse(result['halted'], result)
        self.assertEqual(result['phase'], 'stopped')
        self.assertEqual(result['halt_reason'], '')
        self.assertEqual(held, [True, True])
        self.assertEqual(len(self.client.place_calls), 4)
        self.assertEqual((self.state_dir / 'demo-journal.json').read_bytes(), before[0])
        self.assertTrue(all(row['status'] == 'protected' for row in self.journal()['orders']))
        # The same locks become available only after the coordinator returns.
        with demo._lock(key_path), demo._lock(state_path):
            pass

    def test_service_never_calls_leverage_mutation_even_if_it_would_fail(self):
        self.client.fail_leverage = True
        result = self.run_bot(resume=False)
        self.assertFalse(result['halted'], result)
        self.assertEqual(self.client.leverage_calls, [])
        self.assertEqual(self.client.place_calls, [])
        self.assertFalse(any(delay >= 15 for delay in self.stop.waits))

    def test_resilient_mode_never_recovers_or_resubmits_ambiguous_order(self):
        self.client.fail_ambiguous_market = MARKETS[0]
        self.stop.on_wait = self.advance_bar_on_poll
        result = self.run_bot(cycles=2)
        self.assertTrue(result['halted'])
        self.assertEqual(len(self.client.place_calls), 1)
        self.assertEqual(self.journal()['orders'][0]['status'], 'inspect_required')
        self.assertFalse(any(delay >= 15 for delay in self.stop.waits))

    def test_transient_looking_order_post_failure_is_not_a_read_recovery(self):
        def post_error(_instrument):
            raise OkxError('Order service unavailable', api_code='50013', category='api')
        self.client.after_place = post_error
        self.stop.on_wait = self.advance_bar_on_poll
        result = self.run_bot(cycles=2)
        self.assertTrue(result['halted'])
        self.assertEqual(len(self.client.place_calls), 1)
        self.assertEqual(self.journal()['orders'][0]['status'], 'inspect_required')
        self.assertFalse(any(delay >= 15 for delay in self.stop.waits))

    def test_unverified_accepted_order_read_exhaustion_is_quarantined_not_recovered(self):
        def unavailable(_instrument, _client_id):
            raise OkxError('Order fill verification unavailable', category='timeout')
        self.client.query_order = unavailable
        self.stop.on_wait = self.advance_bar_on_poll
        result = self.run_bot(cycles=2)
        self.assertTrue(result['halted'])
        self.assertEqual(len(self.client.place_calls), 1)
        self.assertEqual(self.journal()['orders'][0]['status'], 'inspect_required')
        self.assertFalse(any(delay >= 15 for delay in self.stop.waits))

    def test_authentication_certificate_data_and_protection_errors_remain_terminal(self):
        errors = [OkxError('safe auth', api_code='50110', category='api'),
                  OkxError('safe TLS', category='tls', tls_failure='certificate'),
                  OkxError('safe response', category='invalid_response')]
        original = self.client.account_config
        for error in errors:
            with self.subTest(category=error.category):
                self.client.account_config = lambda error=error: (_ for _ in ()).throw(error)
                result = self.run_bot()
                self.assertTrue(result['halted'])
                self.assert_no_posts()
                self.assertFalse(any(delay >= 15 for delay in self.stop.waits))
        self.client.account_config = original
        self.client.hide_protection_market = MARKETS[0]
        self.stop.on_wait = self.advance_bar_on_poll
        result = self.run_bot(cycles=2)
        self.assertTrue(result['halted'])
        self.assertEqual(len(self.client.place_calls), 1)
        self.assertEqual(self.journal()['orders'][0]['status'], 'inspect_required')
        self.assertFalse(any(delay >= 15 for delay in self.stop.waits))


if __name__ == '__main__':
    unittest.main()
