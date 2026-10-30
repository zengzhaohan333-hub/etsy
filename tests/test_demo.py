import hashlib
import json
import copy
import tempfile
import threading
import unittest
import uuid
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from okx_bot import demo
from okx_bot.config import Config
from okx_bot.core import CrossStrategy
from okx_bot.okx import AmbiguousOrderError, Candle, Credentials, Instrument, OkxError


D = Decimal
NOW = 1_800_000
INTERVAL = 60_000


def config():
    return SimpleNamespace(instrument="BTC-USDT-SWAP", bar="1m", fast_window=2, slow_window=3,
        initial_equity=D("1000"), risk_per_trade=D("0.005"), max_position_fraction=D("0.1"),
        max_notional=D("100"), stop_loss_pct=D("0.02"), take_profit_pct=D("0.04"),
        max_drawdown_pct=D("0.05"), fee_rate=D("0.0005"), slippage_rate=D("0.0005"))


def candle(ts, close="100"):
    return Candle(ts, D(close), D(close) + 1, D(close) - 1, D(close))


class Signals:
    """All updates signal; runner must ignore historical seed signals."""
    def __init__(self, fast, slow):
        pass

    def update(self, candle):
        return "long"


class NoSleepStop:
    """Event-shaped virtual stop: records backoff without sleeping."""
    def __init__(self, *, stop_on_wait=False):
        self.stopped = False
        self.stop_on_wait = stop_on_wait
        self.waits = []

    def is_set(self):
        return self.stopped

    def set(self):
        self.stopped = True

    def wait(self, seconds):
        self.waits.append(seconds)
        if self.stop_on_wait:
            self.stopped = True
        return self.stopped


class FakeClient:
    def __init__(self, state_dir):
        self.credentials = Credentials("test-" + uuid.uuid4().hex, "test-secret", "test-passphrase")
        self.state_dir = Path(state_dir)
        self.history = [candle(NOW - 5 * INTERVAL + i * INTERVAL) for i in range(4)]
        self.new = candle(self.history[-1].ts + INTERVAL)
        self.candle_calls = 0
        self.place_calls = 0
        self.leverage_calls = 0
        self.position_rows = []
        self.algo_rows = []
        self.order_rows = []
        self.open_order = None
        self.fail_ambiguous = False
        self.hide_protection = False
        self.hide_position = False
        self.add_new = True
        self.account = {"posMode": "net_mode", "acctLv": "2"}
        self.equity = D("1000")
        self.available = D("1000")
        self.metadata = Instrument("BTC-USDT-SWAP", D("0.01"), D("0.01"), D("0.01"), D("10000"))
        self.mark = (D("101"), NOW)

    def account_config(self):
        return self.account

    def server_time(self):
        return NOW

    def instrument(self, inst_id):
        return self.metadata

    def positions(self, inst_id):
        return [] if self.hide_position else self.position_rows

    def pending_orders(self, inst_id):
        return self.order_rows

    def pending_algo_orders(self, inst_id):
        return [] if self.hide_protection else self.algo_rows

    def candles(self, inst_id, bar, limit):
        self.candle_calls += 1
        return self.history + ([self.new] if self.candle_calls > 1 and self.add_new else [])

    def balances(self):
        return {"currency": "USDT", "equity": self.equity, "available": self.available}

    def mark_price(self, inst_id):
        return self.mark

    def set_leverage(self, inst_id, leverage):
        self.leverage_calls += 1
        assert leverage == "1"
        return {"lever": "1", "mgnMode": "isolated"}

    def get_leverage(self, inst_id):
        return [{"instId": inst_id, "lever": "1", "mgnMode": "isolated"}]

    def place_market_order(self, inst_id, side, size, client_id, *, stop_loss, take_profit):
        self.place_calls += 1
        # Meaningful ordering test: an actual persisted intent must already exist.
        journal = json.loads((self.state_dir / "demo-journal.json").read_text())
        assert journal["orders"][-1]["status"] == "intent"
        assert journal["orders"][-1]["client_id"] == client_id
        self.open_order = {"ordId": "order1", "clOrdId": client_id, "instId": inst_id,
                           "state": "filled", "accFillSz": str(size)}
        self.position_rows = [{"instId": inst_id, "posSide": "net", "mgnMode": "isolated",
                               "lever": "1", "pos": str(size if side == "buy" else -size), "avgPx": "100"}]
        self.algo_rows = [{"instId": inst_id, "algoClOrdId": client_id + "a", "sz": str(size),
                           "side": "sell" if side == "buy" else "buy", "posSide": "net", "tdMode": "isolated",
                           "slTriggerPx": str(stop_loss), "tpTriggerPx": str(take_profit),
                           "slOrdPx": "-1", "tpOrdPx": "-1", "slTriggerPxType": "mark", "tpTriggerPxType": "mark"}]
        if self.fail_ambiguous:
            raise AmbiguousOrderError("Order response unavailable; reconcile using client order ID")
        return {"ordId": "order1", "clOrdId": client_id, "sCode": "0"}

    def query_order(self, inst_id, client_id):
        return self.open_order


class DemoTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.state = Path(self.temporary.name)
        self.client = FakeClient(self.state)
        self.clock = patch("okx_bot.demo._now_ms", return_value=NOW)
        self.clock.start()
        self.addCleanup(self.clock.stop)
        self.strategy = patch("okx_bot.demo.CrossStrategy", Signals)
        self.strategy.start()
        self.addCleanup(self.strategy.stop)

    def run_bot(self, cycles=1, stop=None):
        return demo.run_demo(config(), self.state, stop or threading.Event(),
                             poll_seconds=0.001, client=self.client, max_cycles=cycles)

    def journal(self):
        return json.loads((self.state / "demo-journal.json").read_text())

    def run_with_statuses(self, cycles=1, stop=None):
        snapshots = []
        original = demo._save
        def save(path, contents):
            original(path, contents)
            if path.name == "status.json":
                snapshots.append(copy.deepcopy(contents))
        with patch("okx_bot.demo._save", side_effect=save):
            result = self.run_bot(cycles, stop=stop or NoSleepStop())
        return result, snapshots

    @staticmethod
    def connection_states(snapshots):
        values = []
        for state in snapshots:
            connection = state.get("connection")
            values.append(connection.get("state") if isinstance(connection, dict) else connection)
        return values

    def test_no_historical_entry_and_repeated_bar_has_no_order(self):
        self.client.add_new = False
        result = self.run_bot(3)
        self.assertFalse(result["halted"])
        self.assertEqual(self.client.place_calls, 0)
        self.assertEqual(result["entries"], 0)

    def test_transient_balance_read_recovers_and_publishes_recovery_without_orders(self):
        self.client.add_new = False
        original = self.client.balances
        calls = []
        def temporary_failure():
            calls.append(1)
            if len(calls) == 1:
                raise OkxError("Temporary read timeout", category="timeout")
            return original()
        self.client.balances = temporary_failure
        stop = NoSleepStop()
        result, snapshots = self.run_with_statuses(stop=stop)
        self.assertFalse(result["halted"], result)
        self.assertEqual(len(calls), 3)  # failed seed read, retry, successful cycle
        self.assertEqual(self.client.place_calls, 0)
        self.assertEqual(self.client.leverage_calls, 1)
        self.assertEqual(stop.waits, [2])
        states = self.connection_states(snapshots)
        retry = states.index("retrying")
        checking = states.index("checking", retry + 1)
        self.assertIn("connected", states[checking + 1:])
        self.assertEqual(result["phase"], "stopped")
        self.assertEqual(result["last_success_at_ms"], NOW)

    def test_three_balance_read_failures_halt_at_cap_and_preserve_journal(self):
        self.client.add_new = False
        journal_path = self.state / "demo-journal.json"
        original_journal = '{"version":1,"orders":[{"status":"closed","marker":"preserve"}]}'
        journal_path.write_text(original_journal)
        calls = []
        def unavailable():
            calls.append(1)
            raise OkxError("Temporary read timeout", category="timeout")
        self.client.balances = unavailable
        stop = NoSleepStop()
        result = self.run_bot(3, stop=stop)
        self.assertTrue(result["halted"])
        self.assertEqual(result["phase"], "halted")
        self.assertEqual(result["halt_category"], "timeout")
        self.assertEqual(len(calls), 3)
        self.assertEqual(stop.waits, [2, 4])
        self.assertEqual(self.client.leverage_calls, 0)
        self.assertEqual(self.client.place_calls, 0)
        self.assertEqual(journal_path.read_text(), original_journal)

    def test_leverage_write_transport_failure_is_attempted_only_once(self):
        def failed_write(_inst_id, _leverage):
            self.client.leverage_calls += 1
            raise OkxError("Leverage response unavailable", category="timeout")
        self.client.set_leverage = failed_write
        stop = NoSleepStop()
        result = self.run_bot(3, stop=stop)
        self.assertTrue(result["halted"])
        self.assertEqual(self.client.leverage_calls, 1)
        self.assertEqual(self.client.place_calls, 0)
        self.assertEqual(stop.waits, [])

    def test_tls_halts_preserve_safe_subtype_and_read_retry_limit(self):
        for category, subtype, attempts in (("tls", "certificate", 1),
                                             ("tls", "handshake", 1),
                                             ("transport", "unexpected_eof", 3)):
            with self.subTest(subtype=subtype):
                error = OkxError("Static TLS failure", category=category, tls_failure=subtype)
                self.client.balances = Mock(side_effect=error)
                stop = NoSleepStop()
                result, snapshots = self.run_with_statuses(stop=stop)
                self.assertTrue(result["halted"])
                self.assertEqual(result["halt_tls_failure"], subtype)
                self.assertEqual(result["halt_category"], category)
                self.assertEqual(self.client.balances.call_count, attempts)
                self.assertEqual(stop.waits, [2, 4] if attempts == 3 else [])
                self.assertEqual(snapshots[-1]["halt_tls_failure"], subtype)
                self.assertEqual(self.client.place_calls, 0)

    def test_halt_status_omits_inconsistent_or_untrusted_tls_subtype(self):
        for value in ("certificate", "secret-proxy-url", ["secret-proxy-url"]):
            with self.subTest(value=value):
                error = OkxError("Static read failure", category="transport")
                error.tls_failure = value
                self.client.balances = Mock(side_effect=error)
                result = self.run_bot(stop=NoSleepStop())
                self.assertTrue(result["halted"])
                self.assertNotIn("halt_tls_failure", result)
                self.assertEqual(self.client.balances.call_count, 1)

    def test_ambiguous_order_write_with_transient_category_never_retries(self):
        def failed_write(*_args, **_kwargs):
            self.client.place_calls += 1
            self.assertEqual(self.journal()["orders"][-1]["status"], "intent")
            raise AmbiguousOrderError("Order response unavailable", category="timeout")
        self.client.place_market_order = failed_write
        stop = NoSleepStop()
        result = self.run_bot(3, stop=stop)
        self.assertTrue(result["halted"])
        self.assertEqual(self.client.place_calls, 1)
        self.assertEqual(stop.waits, [])
        self.assertEqual(self.journal()["orders"][-1]["status"], "inspect_required")

    def test_fill_query_read_retry_recovers_without_resubmitting_order(self):
        original = self.client.query_order
        queries = []
        def temporary_failure(*args):
            queries.append(1)
            if len(queries) == 1:
                raise OkxError("Temporary order read timeout", category="timeout")
            return original(*args)
        self.client.query_order = temporary_failure
        stop = NoSleepStop()
        result = self.run_bot(stop=stop)
        self.assertFalse(result["halted"], result)
        self.assertEqual(self.client.place_calls, 1)
        self.assertEqual(len(queries), 2)
        self.assertEqual(stop.waits, [2])
        self.assertEqual(result["entries"], 1)
        self.assertEqual(self.journal()["orders"][-1]["status"], "protected")

    def test_exhausted_fill_query_retry_requires_inspection_and_keeps_one_submit(self):
        queries = []
        def unavailable(*_args):
            queries.append(1)
            raise OkxError("Temporary order service unavailable", category="server",
                           api_code="50013", http_status=503)
        self.client.query_order = unavailable
        stop = NoSleepStop()
        result = self.run_bot(3, stop=stop)
        self.assertTrue(result["halted"])
        self.assertEqual(result["phase"], "halted")
        self.assertEqual(result["halt_category"], "server")
        self.assertEqual(result["halt_api_code"], "50013")
        self.assertEqual(result["halt_http_status"], 503)
        self.assertEqual(self.client.place_calls, 1)
        self.assertEqual(len(queries), 3)
        self.assertEqual(stop.waits, [2, 4])
        self.assertEqual(self.journal()["orders"][-1]["status"], "inspect_required")
        self.assertIsNotNone(result["position"])

    def test_stop_during_fill_query_backoff_keeps_unknown_order_for_inspection(self):
        queries = []
        def unavailable(*_args):
            queries.append(1)
            raise OkxError("Temporary order read timeout", category="timeout")
        self.client.query_order = unavailable
        stop = NoSleepStop(stop_on_wait=True)
        result = self.run_bot(3, stop=stop)
        self.assertEqual(len(queries), 1)
        self.assertEqual(self.client.place_calls, 1)
        self.assertEqual(stop.waits, [2])
        self.assertTrue(result["halted"])
        self.assertEqual(self.journal()["orders"][-1]["status"], "inspect_required")
        self.assertIsNotNone(result["position"])

    def test_stop_during_read_backoff_prevents_next_call_and_mutation(self):
        calls = []
        def unavailable():
            calls.append(1)
            raise OkxError("Temporary read timeout", category="timeout")
        self.client.balances = unavailable
        stop = NoSleepStop(stop_on_wait=True)
        result = self.run_bot(3, stop=stop)
        self.assertEqual(len(calls), 1)
        self.assertEqual(stop.waits, [2])
        self.assertEqual(self.client.leverage_calls, 0)
        self.assertEqual(self.client.place_calls, 0)
        self.assertFalse(result["halted"], result)
        self.assertEqual(result["phase"], "stopped")

    def test_virtual_day_keeps_polling_without_reentering_historical_signals(self):
        # This checks a full day of logical polling, not real network uptime.
        interval = 900_000
        initial = 20_000 * 86_400_000
        clock = [initial]
        timestamps = []

        class VirtualStop:
            def is_set(self):
                return False

            def wait(self, seconds):
                clock[0] += int(seconds * 1000)
                return False

        def latest_candles(_instrument, _bar, _limit):
            end = clock[0] // interval * interval
            return [candle(end - (22 - i) * interval) for i in range(22)]

        def remember_status(_path, state):
            timestamps.append(state["updated_at_ms"])

        self.client.candles = latest_candles
        self.client.server_time = lambda: clock[0]
        with patch("okx_bot.demo._now_ms", side_effect=lambda: clock[0]), \
             patch("okx_bot.demo.CrossStrategy", CrossStrategy), \
             patch("okx_bot.demo._save", side_effect=remember_status):
            result = demo.run_demo(Config(), self.state, VirtualStop(), poll_seconds=15,
                                   client=self.client, max_cycles=5761)
        self.assertFalse(result["halted"], result["halt_reason"])
        self.assertEqual(clock[0] - initial, 86_400_000)
        self.assertEqual(timestamps[0], initial)
        self.assertEqual(timestamps[-1], initial + 86_400_000)
        self.assertGreaterEqual(len(timestamps), 5761)
        self.assertEqual(self.client.place_calls, 0)
        self.assertEqual(self.client.leverage_calls, 1)

    def test_normal_entry_is_journaled_then_full_fill_and_bracket_verified(self):
        result = self.run_bot(3)
        self.assertFalse(result["halted"], result["halt_reason"])
        self.assertEqual(result["entries"], 1)
        self.assertEqual(self.client.place_calls, 1)
        self.assertEqual(result["position"]["side"], "long")
        self.assertEqual(self.journal()["orders"][-1]["status"], "protected")
        self.assertIn("Restart is blocked", result["note"])

    def test_existing_position_blocks_all_mutation(self):
        self.client.position_rows = [{"pos": "1", "avgPx": "100"}]
        result = self.run_bot()
        self.assertTrue(result["halted"])
        self.assertIsNotNone(result["position"])
        self.assertEqual(self.client.leverage_calls, 0)
        self.assertEqual(self.client.place_calls, 0)

    def test_stop_during_price_request_does_not_submit(self):
        stop = threading.Event()
        def stop_on_mark(inst_id):
            stop.set()
            return self.client.mark
        self.client.mark_price = stop_on_mark
        result = self.run_bot(stop=stop)
        self.assertFalse(result["halted"], result)
        self.assertEqual(self.client.place_calls, 0)
        self.assertIsNone(result["position"])

    def test_stop_during_durable_intent_does_not_submit(self):
        stop = threading.Event()
        save = demo._save
        def stop_after_intent(path, contents):
            save(path, contents)
            if path.name == 'demo-journal.json' and contents['orders'][-1]['status'] == 'intent':
                stop.set()
        with patch('okx_bot.demo._save', side_effect=stop_after_intent):
            result = self.run_bot(stop=stop)
        self.assertFalse(result["halted"], result)
        self.assertEqual(self.client.place_calls, 0)
        self.assertEqual(self.journal()["orders"][-1]["status"], "closed")
        self.assertTrue(self.journal()["orders"][-1]["never_submitted"])

    def test_signal_expiring_during_mark_read_is_skipped_without_submit(self):
        clock = [NOW]
        def delayed_mark(_inst_id):
            clock[0] = NOW + 45_000
            return D("101"), clock[0]
        self.client.mark_price = delayed_mark
        with patch("okx_bot.demo._now_ms", side_effect=lambda: clock[0]):
            result = self.run_bot(3, stop=NoSleepStop())
        self.assertFalse(result["halted"], result)
        self.assertEqual(self.client.place_calls, 0)
        self.assertEqual(result["skipped_signals"], 1)

    def test_signal_expiring_after_intent_closes_unsent_intent(self):
        clock = [NOW]
        original = demo._save
        def expire_after_intent(path, contents):
            original(path, contents)
            if path.name == "demo-journal.json" and contents["orders"][-1]["status"] == "intent":
                clock[0] = NOW + 45_000
        with patch("okx_bot.demo._now_ms", side_effect=lambda: clock[0]), \
             patch("okx_bot.demo._save", side_effect=expire_after_intent):
            result = self.run_bot(3, stop=NoSleepStop())
        self.assertFalse(result["halted"], result)
        self.assertEqual(self.client.place_calls, 0)
        self.assertEqual(result["skipped_signals"], 1)
        self.assertEqual(self.journal()["orders"][-1]["status"], "closed")
        self.assertTrue(self.journal()["orders"][-1]["never_submitted"])

    def test_mark_expiring_during_intent_write_closes_unsent_intent(self):
        clock = [NOW]
        self.client.mark = (D("101"), NOW - 10_000)
        original = demo._save
        def slow_intent_write(path, contents):
            original(path, contents)
            if path.name == "demo-journal.json" and contents["orders"][-1]["status"] == "intent":
                clock[0] += 25_000
        # Signal and account/flat snapshots are 25s old, but the previously
        # fetched mark is now 35s old and must independently prohibit submit.
        with patch("okx_bot.demo._now_ms", side_effect=lambda: clock[0]), \
             patch("okx_bot.demo._save", side_effect=slow_intent_write):
            result = self.run_bot(3, stop=NoSleepStop())
        self.assertFalse(result["halted"], result)
        self.assertEqual(self.client.place_calls, 0)
        self.assertEqual(result["skipped_signals"], 1)
        self.assertEqual(self.journal()["orders"][-1]["status"], "closed")
        self.assertTrue(self.journal()["orders"][-1]["never_submitted"])

    def test_old_account_snapshot_is_not_used_even_with_fresh_mark_and_signal(self):
        clock = [NOW]
        def delayed_mark(_inst_id):
            clock[0] = NOW + 31_000
            return D("101"), clock[0]
        self.client.mark_price = delayed_mark
        # The signal's 2*poll freshness budget is 120s here, independently of
        # the fixed 30s account/flat-check freshness budget.
        with patch("okx_bot.demo._now_ms", side_effect=lambda: clock[0]):
            result = demo.run_demo(config(), self.state, NoSleepStop(),
                                   poll_seconds=60, client=self.client, max_cycles=1)
        self.assertFalse(result["halted"], result)
        self.assertEqual(result["skipped_signals"], 1)
        self.assertEqual(self.client.place_calls, 0)

    def test_slow_balance_read_does_not_reset_account_snapshot_age(self):
        clock = [NOW]
        original = self.client.balances
        calls = []
        def delayed_balance():
            calls.append(1)
            # Startup succeeds immediately. Only the main cycle's balance
            # request consumes 31s, independently of all later market reads.
            if len(calls) == 2:
                clock[0] += 31_000
            return original()
        self.client.balances = delayed_balance
        self.client.mark_price = lambda _inst_id: (D("101"), clock[0])
        with patch("okx_bot.demo._now_ms", side_effect=lambda: clock[0]):
            result = demo.run_demo(config(), self.state, NoSleepStop(),
                                   poll_seconds=60, client=self.client, max_cycles=1)
        self.assertFalse(result["halted"], result)
        self.assertEqual(result["phase"], "stopped")
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["skipped_signals"], 1)
        self.assertEqual(self.client.place_calls, 0)

    def test_existing_algo_and_pending_orders_block_mutation(self):
        for kind in ("algo_rows", "order_rows"):
            with self.subTest(kind=kind):
                setattr(self.client, kind, [{"external": True}])
                result = self.run_bot()
                self.assertTrue(result["halted"])
                self.assertEqual(self.client.leverage_calls, 0)
                setattr(self.client, kind, [])

    def test_wrong_account_mode_blocks_mutation(self):
        self.client.account["posMode"] = "long_short_mode"
        result = self.run_bot()
        self.assertTrue(result["halted"])
        self.assertEqual(self.client.leverage_calls, 0)

    def test_stale_or_duplicate_or_out_of_order_seed_blocks_leverage(self):
        for history in ([candle(i * INTERVAL) for i in range(4)],
                        [self.client.history[0]] * 4,
                        list(reversed(self.client.history))):
            with self.subTest(history=history):
                original = self.client.history
                self.client.history = history
                self.client.candle_calls = 0
                result = self.run_bot()
                self.assertTrue(result["halted"])
                self.assertEqual(self.client.leverage_calls, 0)
                self.client.history = original

    def test_skipped_new_candle_halts_without_order(self):
        self.client.new = candle(self.client.history[-1].ts + 2 * INTERVAL)
        result = self.run_bot()
        self.assertTrue(result["halted"])
        self.assertEqual(self.client.place_calls, 0)

    def test_ambiguous_submit_persists_reconciliation_block_and_never_retries(self):
        self.client.fail_ambiguous = True
        result = self.run_bot(3)
        self.assertTrue(result["halted"])
        self.assertEqual(self.client.place_calls, 1)
        self.assertEqual(self.journal()["orders"][-1]["status"], "inspect_required")
        self.assertIsNotNone(result["position"])
        # Even if the exchange appears flat later, journal requires explicit review.
        self.client.position_rows = []
        self.client.algo_rows = []
        result = self.run_bot(3)
        self.assertTrue(result["halted"])
        self.assertIn("journal", result["halt_reason"])
        self.assertEqual(self.client.place_calls, 1)

    def test_missing_protection_or_wrong_position_halts_after_one_submit(self):
        for field in ("hide_protection", "hide_position"):
            with self.subTest(field=field):
                local_state = self.state / field
                local_state.mkdir()
                self.client = FakeClient(local_state)
                setattr(self.client, field, True)
                result = demo.run_demo(config(), local_state, threading.Event(), poll_seconds=0.001,
                                       client=self.client, max_cycles=3)
                self.assertTrue(result["halted"])
                self.assertEqual(self.client.place_calls, 1)
                journal = json.loads((local_state / "demo-journal.json").read_text())
                self.assertEqual(journal["orders"][-1]["status"], "inspect_required")

    def test_api_key_wide_lock_prevents_second_runner_in_other_state_dir(self):
        fingerprint = hashlib.sha256(self.client.credentials.api_key.encode()).hexdigest()
        with demo._lock(Path("/tmp") / ("okx-demo-" + fingerprint + ".lock")):
            result = self.run_bot()
        self.assertTrue(result["halted"])
        self.assertIn("Another demo process", result["halt_reason"])
        self.assertEqual(self.client.leverage_calls, 0)
        self.assertEqual(self.client.place_calls, 0)

    def test_stop_before_start_has_no_mutation(self):
        stop = threading.Event()
        stop.set()
        result = self.run_bot(stop=stop)
        self.assertFalse(result["halted"])
        self.assertEqual(self.client.leverage_calls, 0)
        self.assertEqual(self.client.place_calls, 0)

    def test_invalid_or_nonterminal_journal_blocks_all_mutation(self):
        for content in ("not-json", '{"version":1,"orders":[{"status":"intent"}]}'):
            with self.subTest(content=content):
                (self.state / "demo-journal.json").write_text(content)
                result = self.run_bot()
                self.assertTrue(result["halted"])
                self.assertEqual(self.client.leverage_calls, 0)

    def test_sizing_budget_never_exceeds_available_demo_funds(self):
        self.client.available = D("100")
        with patch("okx_bot.demo.position_size", return_value=D("0.1")) as sizing:
            result = self.run_bot()
        self.assertFalse(result["halted"], result["halt_reason"])
        self.assertEqual(sizing.call_args.args[1], D("100"))
        self.assertEqual(sizing.call_args.args[2], D("101"))

    def test_stale_mark_prevents_order(self):
        self.client.mark = (D("100"), NOW - 30_001)
        result = self.run_bot()
        self.assertTrue(result["halted"])
        self.assertIn("Mark price", result["halt_reason"])
        self.assertEqual(self.client.place_calls, 0)

    def test_late_new_signal_does_not_execute(self):
        # The broad bar-history freshness check passes; execution's tighter
        # signal freshness check skips once, without halting or chasing it later.
        clock = [NOW]
        original = self.client.candles
        def delayed_candles(*args):
            candles = original(*args)
            if self.client.candle_calls > 1:
                clock[0] = NOW + 45_000
            return candles
        self.client.candles = delayed_candles
        with patch("okx_bot.demo._now_ms", side_effect=lambda: clock[0]):
            result = self.run_bot(3, stop=NoSleepStop())
        self.assertFalse(result["halted"], result)
        self.assertEqual(result["skipped_signals"], 1)
        self.assertEqual(self.client.candle_calls, 4)
        self.assertEqual(self.client.place_calls, 0)

    def test_status_file_reports_actual_result(self):
        result = self.run_bot()
        status = json.loads((self.state / "status.json").read_text())
        self.assertEqual(status, result)
        self.assertEqual(status["entries"], 1)

    def test_reconcile_requires_flat_and_terminal_order_and_does_not_submit(self):
        self.run_bot()
        first_calls = self.client.place_calls
        result = demo.reconcile_demo(config(), self.state, self.client)
        self.assertTrue(result["halted"])
        self.assertEqual(self.journal()["orders"][-1]["status"], "protected")
        self.client.position_rows = []
        self.client.algo_rows = []
        result = demo.reconcile_demo(config(), self.state, self.client)
        self.assertFalse(result["halted"], result["halt_reason"])
        self.assertTrue(result["reconciled"])
        self.assertEqual(result["closed"], 1)
        self.assertEqual(self.journal()["orders"][-1]["status"], "closed")
        self.assertEqual(self.client.place_calls, first_calls)

    def test_reconcile_does_not_clear_partial_or_unknown_orders(self):
        self.run_bot()
        self.client.position_rows = []
        self.client.algo_rows = []
        self.client.open_order["state"] = "partially_filled"
        result = demo.reconcile_demo(config(), self.state, self.client)
        self.assertTrue(result["halted"])
        self.assertEqual(self.journal()["orders"][-1]["status"], "protected")

    def test_session_drawdown_halts_new_entries(self):
        calls = 0
        original = self.client.balances
        def balances():
            nonlocal calls
            calls += 1
            if calls > 1:
                self.client.equity = D("900")
            return original()
        self.client.balances = balances
        result = self.run_bot()
        self.assertTrue(result["halted"])
        self.assertIn("drawdown", result["halt_reason"])
        self.assertEqual(self.client.place_calls, 0)


if __name__ == "__main__":
    unittest.main()
