import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

from okx_bot.config import Config, load_config
from okx_bot.core import CrossStrategy, position_size, protection_prices
from okx_bot.okx import Candle
from okx_bot.paper import PaperEngine, run_paper
from okx_bot.state import exclusive_lock


def candle(ts, price, low=None, high=None, open_=None):
    p = D(str(price))
    return Candle(ts, D(str(open_)) if open_ is not None else p,
                  D(str(high)) if high is not None else p,
                  D(str(low)) if low is not None else p, p)


class ConfigTests(unittest.TestCase):
    def test_example_config(self):
        self.assertEqual(load_config(Path("config.paper.toml")), Config())

    def test_invalid_finite_and_ranges(self):
        for changes in ({"risk_per_trade": D("NaN")}, {"max_position_fraction": D(2)},
                        {"fee_rate": D(-1)}, {"fast_window": True}, {"slow_window": 201},
                        {"instrument": "BTC-USD-SWAP"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(Config(), **changes)

    def test_unknown_settings_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.toml"
            path.write_text('live = true\n')
            with self.assertRaisesRegex(ValueError, "live"):
                load_config(path)


class StrategyRiskTests(unittest.TestCase):
    def test_crossing_not_initial_alignment_and_no_duplicate(self):
        s = CrossStrategy(2, 3)
        signals = [s.update(candle(n, value)) for n, value in enumerate([3, 2, 1, 2, 4])]
        self.assertEqual(signals, [None, None, None, None, "long"])
        self.assertIsNone(s.update(candle(4, 1)))
        self.assertIsNone(s.update(candle(2, 1)))

    def test_contract_units_caps_and_lot_round_down(self):
        c = Config()
        size = position_size(c, D(1000), D(60000), D("0.01"), D("0.01"), D("0.01"), D(100))
        self.assertEqual(size, D("0.16"))  # 0.16 contracts * .01 BTC * 60000 = 96 USDT
        self.assertLessEqual(size * D("0.01") * D(60000), D(100))

    def test_risk_budget_includes_costs(self):
        c = replace(Config(), max_position_fraction=D(1), max_notional=D(100000), risk_per_trade=D("0.001"))
        size = position_size(c, D(1000), D(100), D(1), D("0.001"), D("0.001"), D(1000))
        assumed_loss = size * D(100) * (c.stop_loss_pct + 2 * (c.fee_rate + c.slippage_rate))
        self.assertLessEqual(assumed_loss, D(1))
        self.assertEqual(position_size(c, D(1), D(100000), D(1), D(1), D(1), D(100)), 0)

    def test_protection_rounds_stop_toward_entry(self):
        c = Config()
        sl, tp = protection_prices(c, "long", D("100.03"), D("0.1"))
        self.assertLess(sl, D("100.03"))
        self.assertGreaterEqual(sl, D("100.03") * D("0.99"))
        sl, tp = protection_prices(c, "short", D("100.03"), D("0.1"))
        self.assertGreater(sl, D("100.03"))
        self.assertLessEqual(sl, D("100.03") * D("1.01"))

    def test_short_exit_fee_on_larger_stop_notional_is_budgeted(self):
        c = replace(Config(), risk_per_trade=D("0.001"), stop_loss_pct=D("0.5"),
                    fee_rate=D("0.01"), slippage_rate=D(0),
                    max_position_fraction=D(1), max_notional=D(100000))
        size = position_size(c, D(1000), D(100), D(1), D("0.000001"), D("0.000001"), D(1000))
        actual_stop_loss = size * (D(50) + D(100) * c.fee_rate + D(150) * c.fee_rate)
        self.assertLessEqual(actual_stop_loss, D(1))


class PaperTests(unittest.TestCase):
    def setUp(self):
        self.config = replace(Config(), fee_rate=D(0), slippage_rate=D(0),
                              max_notional=D(1000), max_position_fraction=D(1))

    def test_signal_executes_only_on_following_bar(self):
        engine = PaperEngine(replace(self.config, fast_window=2, slow_window=3))
        for i, value in enumerate([103, 102, 101, 102, 104]):
            engine.process(candle(i * 900000, value))
        self.assertEqual(engine.pending, "long")
        self.assertIsNone(engine.position)
        engine.process(candle(5 * 900000, 104))
        self.assertEqual(engine.position.entry_price, D(104))

    def test_stop_wins_unknown_intrabar_path(self):
        engine = PaperEngine(self.config)
        engine.open("long", D(100), 0)
        engine.process(candle(900000, 100, low=98, high=103))
        self.assertEqual(engine.trades[0]["exit_price"], "99.00")
        self.assertEqual(engine.trades[0]["reason"], "stop_loss")

    def test_gap_stop_fills_worse_price(self):
        engine = PaperEngine(self.config)
        engine.open("short", D(100), 0)
        engine.process(candle(900000, 105))
        self.assertEqual(D(engine.trades[0]["exit_price"]), D(105))

    def test_reversal_at_open_precedes_later_intrabar_stop(self):
        engine = PaperEngine(self.config)
        engine.open("long", D(100), 0)
        engine.pending = "short"
        engine.process(candle(900000, 100, low=98, high=103))
        self.assertEqual(engine.trades[0]["reason"], "signal_reversal")
        self.assertEqual(D(engine.trades[0]["exit_price"]), D(100))

    def test_both_sides_pnl_and_round_trip_costs(self):
        for side, exit_price in (("long", D(110)), ("short", D(90))):
            engine = PaperEngine(Config())
            engine.open(side, D(100), 0)
            engine.close(exit_price, 1, "test")
            pnl = sum(D(t["pnl"]) for t in engine.trades)
            self.assertEqual(engine.cash, engine.config.initial_equity + pnl)
            self.assertGreater(engine.fees, 0)
            self.assertGreater(pnl, 0)

    def test_drawdown_closes_and_prevents_further_entries(self):
        c = replace(self.config, initial_equity=D(1000), max_drawdown_pct=D("0.001"))
        engine = PaperEngine(c)
        engine.open("long", D(100), 0)
        engine.process(candle(900000, 90))
        self.assertTrue(engine.halted)
        self.assertIsNone(engine.position)
        engine.open("long", D(100), 1800000)
        self.assertIsNone(engine.position)

    def test_duplicate_bar_and_missing_bar(self):
        engine = PaperEngine(self.config)
        engine.process(candle(0, 100))
        engine.process(candle(0, 100))
        self.assertEqual(engine.processed, 1)
        with self.assertRaisesRegex(ValueError, "不连续"):
            engine.process(candle(1800000, 100))

    def test_replay_writes_trades_reconciles_and_locks(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            result = run_paper(Config(), directory, 320)
            self.assertEqual(result["processed_bars"], 320)
            self.assertGreater(result["total_trades"], 5)
            self.assertIsNone(result["position"])
            self.assertEqual(D(result["cash"]), D(10000) + sum(D(t["pnl"]) for t in result["trades"]))
            self.assertTrue((directory / "trades.jsonl").read_text().strip())
            self.assertTrue((directory / "status.json").is_file())
            with exclusive_lock(directory / "paper.lock"):
                with self.assertRaises(RuntimeError):
                    run_paper(Config(), directory)


if __name__ == "__main__":
    unittest.main()
