"""Execution fixtures use controlled signals; integration tests use the real SMA."""
import json
import unittest
from dataclasses import replace
from decimal import Decimal as D
from unittest.mock import patch

from okx_bot.backtest_engine import BAR_MS, run_backtest
from okx_bot.config import Config
from okx_bot.okx import Candle, Instrument


START = 10 * BAR_MS


def bar(ts, close="100", *, open_=None, low=None, high=None):
    c = D(str(close))
    o = D(str(open_)) if open_ is not None else c
    lo = D(str(low)) if low is not None else min(o, c)
    hi = D(str(high)) if high is not None else max(o, c)
    return Candle(ts, o, hi, lo, c)


def base_config(**changes):
    initial = replace(Config(), fast_window=2, slow_window=3,
                      initial_equity=D("1000"), max_notional=D("100"),
                      max_position_fraction=D(1), risk_per_trade=D("0.02"),
                      stop_loss_pct=D("0.10"), take_profit_pct=D("0.20"),
                      fee_rate=D(0), slippage_rate=D(0))
    return replace(initial, **changes)


def metadata(**changes):
    inst = Instrument("BTC-USDT-SWAP", D(1), D("0.01"), D("0.01"), D("100000"), D("0.01"))
    return replace(inst, **changes)


def seed(prices=(100, 100, 100, 100)):
    return [bar(START - (len(prices) - i) * BAR_MS, p) for i, p in enumerate(prices)]


class BacktestTests(unittest.TestCase):
    def run_fixture(self, measured=None, *, marks=None, funding=None, signals=None,
                    config=None, instrument=None, warmup=None, **kwargs):
        measured = measured or [bar(START + i * BAR_MS) for i in range(4)]
        trade = (warmup if warmup is not None else seed()) + measured
        mark = (warmup if warmup is not None else seed()) + (marks if marks is not None else measured)
        options = dict(starting_equity=D(1000), start_ms=START,
                       end_ms=measured[-1].ts + BAR_MS, **kwargs)
        def perform():
            return run_backtest(config or base_config(), trade, mark, funding or [],
                                instrument or metadata(), **options)
        if signals is None:
            return perform()
        class ScheduledSignals:
            def __init__(self, fast, slow):
                pass
            def update(self, candle):
                return signals.get(candle.ts)
        with patch('okx_bot.backtest_engine.CrossStrategy', ScheduledSignals):
            return perform()

    def test_real_sma_signal_executes_next_open_without_lookahead(self):
        measured = [bar(START, 104, open_=102), bar(START + BAR_MS, 110)]
        result = self.run_fixture(measured, warmup=seed((103, 102, 101, 102)))
        self.assertEqual(result['entries'], 1)
        self.assertEqual(result['open_position']['entry_time'], START + BAR_MS)
        self.assertEqual(D(result['open_position']['entry_price']), D(110))

    def test_warmup_crossing_is_ignored(self):
        measured = [bar(START + i * BAR_MS, 103 + i) for i in range(3)]
        result = self.run_fixture(measured, warmup=seed((100, 99, 100, 102)))
        self.assertEqual(result['entries'], 0)
        self.assertEqual(D(result['net_pnl']), 0)

    def test_only_last_warmup_and_current_segment_ohlc_are_read(self):
        c = base_config()
        ordinary = seed() + [bar(START + i * BAR_MS) for i in range(3)]
        arguments = dict(starting_equity=D(1000), start_ms=START, end_ms=START + 3 * BAR_MS)
        expected = run_backtest(c, ordinary, ordinary, [], metadata(), **arguments)
        unreadable = Candle(START - 5 * BAR_MS, D('NaN'), D('NaN'), D('NaN'), D('NaN'))
        future = Candle(START + 3 * BAR_MS, D('NaN'), D('NaN'), D('NaN'), D('NaN'))
        full = [unreadable] + ordinary + [future]
        self.assertEqual(run_backtest(c, full, full, [], metadata(), **arguments), expected)

    def test_crossings_while_holding_never_reverse_or_add(self):
        result = self.run_fixture(signals={START: 'long', START + BAR_MS: 'short', START + 2 * BAR_MS: 'long'})
        self.assertEqual(result['entries'], 1)
        self.assertEqual(result['closed_trades'], 0)
        self.assertEqual(result['open_position']['side'], 'long')

    def test_held_signal_is_not_reused_after_next_open_gap_exit(self):
        measured = [bar(START), bar(START + BAR_MS), bar(START + 2 * BAR_MS, 75), bar(START + 3 * BAR_MS, 75)]
        marks = [bar(START), bar(START + BAR_MS), bar(START + 2 * BAR_MS, 80), bar(START + 3 * BAR_MS, 80)]
        result = self.run_fixture(measured, marks=marks, signals={START: 'long', START + BAR_MS: 'short'})
        self.assertEqual(result['entries'], 1)
        self.assertEqual(result['closed_trades'], 1)
        self.assertIsNone(result['open_position'])
        self.assertEqual(result['trades'][0]['reason'], 'gap_stop_loss')
        self.assertEqual(D(result['trades'][0]['exit_price']), 75)

    def test_same_bar_both_protections_assume_stop_first(self):
        measured = [bar(START), bar(START + BAR_MS, low=80, high=130)]
        for side, expected in (('long', D(90)), ('short', D(110))):
            with self.subTest(side=side):
                result = self.run_fixture(measured, signals={START: side})
                self.assertEqual(result['trades'][0]['reason'], 'stop_loss')
                self.assertEqual(D(result['trades'][0]['exit_price']), expected)
                self.assertEqual(result['trades'][0]['exit_time_precision'], 'bar')
                self.assertEqual(result['trades'][0]['exit_time'], START + 2 * BAR_MS)

    def test_short_gap_stop_uses_adverse_trade_open(self):
        measured = [bar(START), bar(START + BAR_MS), bar(START + 2 * BAR_MS, 130)]
        marks = [bar(START), bar(START + BAR_MS), bar(START + 2 * BAR_MS, 120)]
        result = self.run_fixture(measured, marks=marks, signals={START: 'short'})
        self.assertEqual(result['trades'][0]['reason'], 'gap_stop_loss')
        self.assertEqual(D(result['trades'][0]['exit_price']), D(130))
        self.assertEqual(D(result['trades'][0]['net_pnl']), D(-30))

    def test_only_mark_range_triggers_protection(self):
        measured = [bar(START), bar(START + BAR_MS, low=50, high=150)]
        marks = [bar(START), bar(START + BAR_MS, low=99, high=101)]
        result = self.run_fixture(measured, marks=marks, signals={START: 'long'})
        self.assertEqual(result['closed_trades'], 0)
        self.assertIsNotNone(result['open_position'])

    def test_intrabar_reference_is_clamped_to_observed_trade_range(self):
        for trade_exit, mark_exit, expected in (
                (bar(START + 2 * BAR_MS, 55, low=50, high=60),
                 bar(START + 2 * BAR_MS, 100, low=85, high=101), D(60)),
                (bar(START + 2 * BAR_MS, 105, low=100, high=110),
                 bar(START + 2 * BAR_MS, 100, low=99, high=125), D(110))):
            with self.subTest(expected=expected):
                measured = [bar(START), bar(START + BAR_MS), trade_exit]
                marks = [bar(START), bar(START + BAR_MS), mark_exit]
                result = self.run_fixture(measured, marks=marks, signals={START: 'long'})
                fill = D(result['trades'][0]['exit_price'])
                self.assertEqual(fill, expected)
                self.assertLessEqual(fill, trade_exit.high)
                self.assertGreaterEqual(fill, trade_exit.low)

    def test_funding_sign_and_exact_entry_and_end_timing(self):
        measured = [bar(START + i * BAR_MS) for i in range(3)]
        for side, rate, expected in (('long', D('.01'), D(-1)), ('short', D('.01'), D(1)),
                                     ('long', D('-.01'), D(1)), ('short', D('-.01'), D(-1))):
            with self.subTest(side=side, rate=rate):
                funding = [(START + BAR_MS, D('.9')), (START + 2 * BAR_MS, rate),
                           (START + 3 * BAR_MS, D('.9'))]
                result = self.run_fixture(measured, funding=funding, signals={START: side})
                self.assertEqual(D(result['funding_pnl']), expected)
                self.assertEqual(D(result['open_position']['funding_pnl']), expected)
                self.assertEqual(result['applied_funding_events'], 1)
                self.assertEqual(result['funding_events'], 2)

    def test_funding_is_allocated_before_gap_exit_at_mark_open_notional(self):
        measured = [bar(START), bar(START + BAR_MS), bar(START + 2 * BAR_MS, 75)]
        marks = [bar(START), bar(START + BAR_MS), bar(START + 2 * BAR_MS, 80)]
        result = self.run_fixture(measured, marks=marks,
                                  funding=[(START + 2 * BAR_MS, D('.01'))], signals={START: 'long'})
        self.assertEqual(D(result['funding_pnl']), D('-.8'))
        self.assertEqual(D(result['trades'][0]['funding_pnl']), D('-.8'))
        self.assertEqual(D(result['trades'][0]['net_pnl']), D('-25.8'))
        self.assertEqual(D(result['net_pnl']), D('-25.8'))

    def test_fees_slippage_and_trade_net_pnl_reconcile(self):
        measured = [bar(START), bar(START + BAR_MS), bar(START + 2 * BAR_MS, high=125)]
        c = base_config(fee_rate=D('.0005'), slippage_rate=D('.001'))
        result = self.run_fixture(measured, config=c, signals={START: 'long'})
        trade = result['trades'][0]
        self.assertEqual(D(trade['entry_price']), D('100.100'))
        self.assertEqual(D(trade['exit_price']), D('119.88000'))
        q = D(trade['quantity'])
        expected_fees = (D(trade['entry_price']) + D(trade['exit_price'])) * q * c.fee_rate
        self.assertEqual(D(result['fees']), expected_fees)
        self.assertEqual(D(trade['net_pnl']), D(trade['gross_pnl']) - expected_fees)
        self.assertEqual(D(result['ending_equity']), D(1000) + D(trade['net_pnl']))

    def test_cost_multipliers_apply_to_fills_and_sizing(self):
        measured = [bar(START), bar(START + BAR_MS), bar(START + 2 * BAR_MS, high=125)]
        c = base_config(fee_rate=D('.0005'), slippage_rate=D('.001'))
        base = self.run_fixture(measured, config=c, signals={START: 'long'})
        stress = self.run_fixture(measured, config=c, signals={START: 'long'},
                                   fee_multiplier=D(2), slippage_multiplier=D(2))
        self.assertLess(D(stress['net_pnl']), D(base['net_pnl']))
        self.assertEqual(D(stress['fee_rate']), D('.001'))
        self.assertEqual(D(stress['slippage_rate']), D('.002'))

    def test_drawdown_stops_entries_but_keeps_position_and_settlements(self):
        measured = [bar(START), bar(START + BAR_MS), bar(START + 2 * BAR_MS, 95), bar(START + 3 * BAR_MS, 95)]
        c = base_config(max_drawdown_pct=D('.004'))
        result = self.run_fixture(measured, config=c, signals={START: 'long'},
                                  funding=[(START + 3 * BAR_MS, D('.01'))])
        self.assertTrue(result['halted'])
        self.assertEqual(result['entries'], 1)
        self.assertEqual(result['closed_trades'], 0)
        self.assertIsNotNone(result['open_position'])
        self.assertEqual(D(result['funding_pnl']), D('-.95'))
        self.assertEqual(result['halted_at_ms'], START + 2 * BAR_MS)

    def test_protection_still_executes_after_drawdown_halt_and_never_reenters(self):
        measured = [bar(START), bar(START + BAR_MS), bar(START + 2 * BAR_MS, 95),
                    bar(START + 3 * BAR_MS, 85), bar(START + 4 * BAR_MS, 85)]
        c = base_config(max_drawdown_pct=D('.004'))
        result = self.run_fixture(measured, config=c,
                                  signals={START: 'long', START + 3 * BAR_MS: 'short'})
        self.assertTrue(result['halted'])
        self.assertEqual(result['entries'], 1)
        self.assertEqual(result['closed_trades'], 1)
        self.assertEqual(result['trades'][0]['reason'], 'gap_stop_loss')
        self.assertIsNone(result['open_position'])

    def test_end_does_not_force_liquidation_and_pnl_includes_open_entry_fee(self):
        c = base_config(fee_rate=D('.001'))
        result = self.run_fixture(config=c, signals={START: 'long'})
        self.assertEqual(result['closed_trades'], 0)
        self.assertIsNotNone(result['open_position'])
        self.assertEqual(D(result['net_pnl']), -D(result['open_position']['entry_fee']))
        self.assertEqual(D(result['fees']), D(result['open_position']['entry_fee']))
        self.assertIsNone(result['profit_factor'])
        self.assertIsNone(result['win_rate_pct'])
        json.dumps(result, allow_nan=False)  # all monetary values must be JSON-safe

    def test_closed_and_open_net_pnl_reconcile_with_both_funding_directions(self):
        measured = [bar(START), bar(START + BAR_MS), bar(START + 2 * BAR_MS, high=125),
                    bar(START + 3 * BAR_MS), bar(START + 4 * BAR_MS)]
        c = base_config(fee_rate=D('.001'))
        result = self.run_fixture(measured, config=c,
            signals={START: 'long', START + 2 * BAR_MS: 'short'},
            funding=[(START + 2 * BAR_MS, D('.01')), (START + 4 * BAR_MS, D('.01'))])
        self.assertEqual(result['entries'], 2)
        self.assertEqual(result['closed_trades'], 1)
        self.assertEqual(D(result['trades'][0]['funding_pnl']), D(-1))
        self.assertEqual(D(result['open_position']['funding_pnl']), D(1))
        combined = sum((D(t['net_pnl']) for t in result['trades']), D(0)) + D(result['open_position']['net_pnl'])
        self.assertEqual(D(result['ending_equity']) - D(result['initial_equity']), combined)
        self.assertEqual(D(result['funding_pnl']), D(0))

    def test_contract_steps_and_cash_cap_are_honored_with_price_basis(self):
        measured = [bar(START), bar(START + BAR_MS, 110)]
        marks = [bar(START), bar(START + BAR_MS, 100)]
        result = self.run_fixture(measured, marks=marks, signals={START: 'long'},
                                  instrument=metadata(ct_val=D('.01'), lot_size=D('.1'), min_size=D('.1')))
        p = result['open_position']
        size, value = D(p['contracts']), D(p['quantity']) * D(p['entry_price'])
        self.assertEqual(size % D('.1'), 0)
        self.assertLessEqual(value, D(100))
        self.assertEqual(D(p['quantity']), size * D('.01'))

    def test_missing_duplicate_unaligned_or_mismatched_bars_are_rejected(self):
        ordinary = seed() + [bar(START + i * BAR_MS) for i in range(3)]
        cases = [ordinary[:-1], ordinary[:4] + ordinary[5:], ordinary[:5] + [ordinary[4]] + ordinary[5:],
                 ordinary[:-1] + [replace(ordinary[-1], ts=ordinary[-1].ts + 1)]]
        for trade in cases:
            with self.subTest(timestamps=[b.ts for b in trade]), self.assertRaises(ValueError):
                run_backtest(base_config(), trade, ordinary, [], metadata(), starting_equity=D(1000),
                             start_ms=START, end_ms=START + 3 * BAR_MS)

    def test_interval_warmup_metadata_and_funding_validation(self):
        for config, start, end, funding, inst in (
                (base_config(bar='5m'), START, START + 3 * BAR_MS, [], metadata()),
                (base_config(), START + 1, START + 3 * BAR_MS, [], metadata()),
                (base_config(), START, START, [], metadata()),
                (base_config(), START, START + 3 * BAR_MS, [(START + 1, D('.01'))], metadata()),
                (base_config(), START, START + 3 * BAR_MS, [(START, D('.01')), (START, D('.01'))], metadata()),
                (base_config(), START, START + 3 * BAR_MS, [(START, D('NaN'))], metadata()),
                (base_config(), START, START + 3 * BAR_MS, [], metadata(inst_id='ETH-USDT-SWAP'))):
            with self.subTest(config=config, start=start, funding=funding), self.assertRaises(ValueError):
                bars = seed() + [bar(START + i * BAR_MS) for i in range(3)]
                run_backtest(config, bars, bars, funding, inst, starting_equity=D(1000), start_ms=start, end_ms=end)
        with self.assertRaisesRegex(ValueError, 'warm-up'):
            self.run_fixture(warmup=seed((100, 100, 100)))


if __name__ == '__main__':
    unittest.main()
