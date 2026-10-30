"""Orchestration checks with synthetic fixtures, never real exchange requests."""
import contextlib
import copy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from okx_bot import backtest
from okx_bot.backtest_report import render_report
from okx_bot.config import Config
from okx_bot.history import HistoryDataset, load_dataset, save_dataset
from okx_bot.okx import Candle, Instrument


INTERVAL = 900_000


def dataset_fixture(config=None):
    """A complete one-day fixture, including real-schema funding anchors."""
    config = config or Config()
    interval = backtest.bar_milliseconds(config.bar)
    count = 86_400_000 // interval
    warmup = config.slow_window + 1
    start = 86_400_000
    end = start + 86_400_000
    first = start - warmup * interval
    pattern = [0, 300, 700, 1200, 700, 300, 0, -300, -700, -1200, -700, -300]
    candles = []
    previous = D(60_000)
    for index in range(count + warmup):
        close = D(60_000 + pattern[index % len(pattern)])
        candles.append(Candle(first + index * interval, previous,
                              max(previous, close) + 10,
                              min(previous, close) - 10, close))
        previous = close
    instrument = Instrument(config.instrument, D(".01"), D(".01"),
                            D(".01"), D(10000), D(".1"), config.instrument.split('-')[0])
    funding = [(start + offset * 28_800_000, D(".0001")) for offset in (0, 1, 2)]
    return HistoryDataset(candles, list(candles), funding, instrument,
                          start, end, end + interval,
                          config.bar, warmup,
                          (start - 28_800_000, D(".0001")),
                          (end, D(".0001")))


def fake_result(_config, _trade, _mark, funding, _instrument, **kwargs):
    """Record-sensitive result, without replacing time/risk logic in the engine."""
    return {"start_ms": kwargs["start_ms"], "end_ms": kwargs["end_ms"],
            "initial_equity": str(kwargs["starting_equity"]),
            "closed_trades": 2, "halted": False, "halted_at_ms": None,
            "net_pnl": "0", "return_pct": "0", "max_drawdown_pct": "0",
            "funding_pnl": str(sum((rate for _, rate in funding), D(0))),
            "equity_curve": [], "trades": [], "open_position": None}


class ReportOrchestrationTests(unittest.TestCase):
    def setUp(self):
        self.config = Config()
        self.dataset = dataset_fixture(self.config)

    def test_chronological_segments_reset_capital_and_stress_costs_only(self):
        config_before = copy.deepcopy(self.config)
        dataset_before = copy.deepcopy(self.dataset)
        with patch("okx_bot.backtest.run_backtest", side_effect=fake_result) as engine:
            report = backtest.build_report(self.config, self.dataset, D(5000), "a" * 64)
        self.assertEqual(engine.call_count, 4)
        split = self.dataset.start_ms + 67 * INTERVAL  # floor(96 * .7)
        expected_intervals = [(self.dataset.start_ms, self.dataset.end_ms),
                              (self.dataset.start_ms, split),
                              (split, self.dataset.end_ms),
                              (split, self.dataset.end_ms)]
        for index, call in enumerate(engine.call_args_list):
            self.assertIs(call.args[0], self.config)
            self.assertEqual((call.kwargs["start_ms"], call.kwargs["end_ms"]),
                             expected_intervals[index])
            self.assertEqual(call.kwargs["starting_equity"], D(5000))
            multiplier = D(2) if index == 3 else D(1)
            self.assertEqual(call.kwargs["fee_multiplier"], multiplier)
            self.assertEqual(call.kwargs["slippage_multiplier"], multiplier)
            for ts, rate in call.args[3]:
                self.assertTrue(expected_intervals[index][0] <= ts < expected_intervals[index][1])
                self.assertEqual(rate, D(".0001"))
        self.assertEqual(engine.call_args_list[2].args[3], engine.call_args_list[3].args[3])
        self.assertEqual(self.config, config_before)
        self.assertEqual(self.dataset, dataset_before)
        self.assertEqual(report["split"], {"ratio": "0.7", "train_end_ms": split})
        self.assertEqual(list(report["results"]), ["full", "train", "test", "test_double_costs"])
        self.assertEqual(report["dataset_sha256"], "a" * 64)
        self.assertIn("后段已平仓交易少于 30 笔", " ".join(report["warnings"]))

    def test_each_halted_segment_is_disclosed_without_implying_flattening(self):
        def halted_result(*args, **kwargs):
            result = fake_result(*args, **kwargs)
            result.update(halted=True, halted_at_ms=kwargs["start_ms"],
                          open_position={"side": "long", "quantity": ".001"})
            return result
        with patch("okx_bot.backtest.run_backtest", side_effect=halted_result):
            report = backtest.build_report(self.config, self.dataset, D(5000), "b" * 64)
        warnings = " ".join(report["warnings"])
        for label in ("全区间", "前段", "后段", "后段双倍成本"):
            self.assertIn(label + "触发回撤停止", warnings)
        self.assertIn("已有保护单与资金费继续模拟", warnings)
        self.assertTrue(all(result["open_position"] for result in report["results"].values()))

    def test_mismatched_dataset_or_insufficient_warmup_refuses_engine(self):
        invalid = (replace(self.dataset, bar="1m"),
                   replace(self.dataset, warmup_bars=self.config.slow_window),
                   replace(self.dataset, end_ms=self.dataset.start_ms + 69 * INTERVAL))
        for dataset in invalid:
            with self.subTest(dataset=dataset), patch("okx_bot.backtest.run_backtest") as engine:
                with self.assertRaises(backtest.BacktestSetupError):
                    backtest.build_report(self.config, dataset, D(5000), "c" * 64)
                engine.assert_not_called()

    def test_real_engine_output_renders_actual_trade_exit_times(self):
        config = replace(self.config, fast_window=2, slow_window=3,
                         stop_loss_pct=D(".005"), take_profit_pct=D(".006"))
        dataset = dataset_fixture(config)
        report = backtest.build_report(config, dataset, D(5000), "d" * 64)
        html = render_report(report)
        trades = report["results"]["test"]["trades"]
        self.assertGreater(len(trades), 0)
        for trade in trades[-20:]:
            text = datetime.fromtimestamp(trade["exit_time"] / 1000,
                timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M")
            self.assertIn(f"<td>{text}<small>", html)
        self.assertIn("UTC+8", html)
        for result in report["results"].values():
            closed = sum((D(trade["net_pnl"]) for trade in result["trades"]), D(0))
            open_pnl = D(result["open_position"]["net_pnl"]) if result["open_position"] else D(0)
            self.assertEqual(D(result["ending_equity"]) - D(result["initial_equity"]), closed + open_pnl)

    def test_five_minute_split_uses_288_bars_and_does_not_leak_boundary_funding(self):
        config = replace(self.config, bar="5m")
        dataset = dataset_fixture(config)
        interval = 300_000
        split = dataset.start_ms + 201 * interval  # floor(288 * .7)
        funding = sorted([*dataset.funding_rates, (split, D(".0002"))])
        dataset = replace(dataset, funding_rates=funding)
        with patch("okx_bot.backtest.run_backtest", side_effect=fake_result) as engine:
            report = backtest.build_report(config, dataset, D(5000), "e" * 64)
        self.assertEqual(report["source"]["bar"], "5m")
        self.assertEqual(report["split"]["train_end_ms"], split)
        self.assertEqual(engine.call_count, 4)
        self.assertEqual((engine.call_args_list[1].kwargs["start_ms"], engine.call_args_list[1].kwargs["end_ms"]),
                         (dataset.start_ms, split))
        self.assertNotIn((split, D(".0002")), engine.call_args_list[1].args[3])
        self.assertIn((split, D(".0002")), engine.call_args_list[2].args[3])
        self.assertEqual(engine.call_args_list[2].kwargs["starting_equity"], D(5000))
        self.assertEqual(engine.call_args_list[3].kwargs["fee_multiplier"], D(2))
        self.assertIn("5 分钟 OHLC", " ".join(report["warnings"]))
        self.assertNotIn("15 分钟 OHLC", " ".join(report["warnings"]))

    def test_five_minute_reports_are_separate_per_market_not_aggregate_equity(self):
        for instrument in ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "XRP-USDT-SWAP"):
            with self.subTest(instrument=instrument):
                config = replace(self.config, instrument=instrument, bar="5m")
                report = backtest.build_report(config, dataset_fixture(config), D(5000), "f" * 64)
                self.assertEqual(report["source"]["instrument"], instrument)
                self.assertEqual(report["source"]["bar"], "5m")
                self.assertEqual(report["results"]["test"]["initial_equity"], "5000")
                self.assertIn("已收盘的5分钟", report["results"]["test"]["assumptions"][0])
                self.assertNotIn("portfolio", report)


class BacktestCommandTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / ".runtime-backtest"
        self.dataset = dataset_fixture()

    def invoke(self, extra=()):
        with contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()) as error:
            code = backtest.main(["--state-dir", str(self.state), *extra])
        return code, output.getvalue(), error.getvalue()

    def test_saved_dataset_replays_offline_without_keys_and_preserves_demo(self):
        source = self.root / "source-dataset.json"
        save_dataset(self.dataset, source)
        demo_dir = self.root / ".runtime-demo"
        demo_dir.mkdir()
        sentinel = demo_dir / "demo-journal.json"
        sentinel.write_text('{"do_not_touch":true}', encoding="utf-8")
        config_path = Path("config.paper.toml")
        config_before, source_before, demo_before = (config_path.read_bytes(), source.read_bytes(), sentinel.read_bytes())
        with patch("okx_bot.backtest.fetch_dataset", side_effect=AssertionError("offline must not fetch")) as fetch, \
                patch("okx_bot.okx.Credentials.from_env", side_effect=AssertionError("public backtest must not read keys")) as credentials:
            code, output, error = self.invoke(["--dataset", str(source)])
        self.assertEqual(code, 0, error)
        fetch.assert_not_called()
        credentials.assert_not_called()
        self.assertIn("回测完成", output)
        self.assertEqual(error, "")
        self.assertEqual(sentinel.read_bytes(), demo_before)
        self.assertEqual(source.read_bytes(), source_before)
        self.assertEqual(config_path.read_bytes(), config_before)
        for name in ("dataset.json", "report.json", "report.html"):
            self.assertTrue((self.state / name).is_file(), name)
        loaded = load_dataset(self.state / "dataset.json")
        self.assertEqual(loaded, self.dataset)
        report = json.loads((self.state / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["source"]["start_ms"], self.dataset.start_ms)
        self.assertEqual(report["results"]["test"]["initial_equity"], "5000")
        self.assertEqual(report["dataset_sha256"], hashlib.sha256((self.state / "dataset.json").read_bytes()).hexdigest())
        self.assertIn("<!doctype html>", (self.state / "report.html").read_text(encoding="utf-8"))
        self.assertFalse((self.state / "demo-journal.json").exists())

    def test_public_download_path_uses_predeclared_default_days(self):
        with patch("okx_bot.backtest.fetch_dataset", return_value=self.dataset) as fetch, \
                patch("okx_bot.backtest.load_dataset") as load:
            code, _, error = self.invoke()
        self.assertEqual(code, 0, error)
        self.assertEqual(fetch.call_count, 1)
        self.assertEqual(fetch.call_args.kwargs["days"], 30)
        load.assert_not_called()

    def test_five_minute_saved_dataset_generates_offline_report_without_changing_default(self):
        config = replace(Config(), bar="5m")
        source = self.root / "source-5m.json"
        save_dataset(dataset_fixture(config), source)
        profile = self.root / "5m.toml"
        profile.write_text('bar = "5m"\n', encoding="utf-8")
        with patch("okx_bot.backtest.fetch_dataset", side_effect=AssertionError("offline must not fetch")):
            code, _, error = self.invoke(["--config", str(profile), "--dataset", str(source)])
        self.assertEqual(code, 0, error)
        report = json.loads((self.state / "report.json").read_text(encoding="utf-8"))
        self.assertEqual(report["source"]["bar"], "5m")
        self.assertIn("5 分钟 OHLC", " ".join(report["warnings"]))
        self.assertEqual(Config().bar, "15m")

    def test_invalid_capital_is_refused_before_any_download_or_file_output(self):
        for value in ("0", "-5", "NaN", "Infinity", "not-a-number"):
            with self.subTest(value=value), patch("okx_bot.backtest.fetch_dataset") as fetch, \
                    patch("okx_bot.backtest.load_dataset") as load:
                with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                    self.invoke(["--capital", value])
                self.assertEqual(error.exception.code, 2)
                fetch.assert_not_called()
                load.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_invalid_setup_is_refused_before_public_request(self):
        configurations = (replace(Config(), bar="1m"),)
        for config in configurations:
            with self.subTest(config=config), patch("okx_bot.backtest.load_config", return_value=config), \
                    patch("okx_bot.backtest.fetch_dataset") as fetch:
                code, _, error = self.invoke()
                self.assertEqual(code, 1)
                self.assertIn("回测未完成", error)
                fetch.assert_not_called()
        for days in ("6", "61"):
            with self.subTest(days=days), patch("okx_bot.backtest.fetch_dataset") as fetch:
                self.assertEqual(self.invoke(["--days", days])[0], 1)
                fetch.assert_not_called()
        self.assertFalse(self.state.exists())

    def test_demo_and_paper_state_directories_are_refused_and_unchanged(self):
        for name in (".runtime", ".runtime-demo", "custom-demo-state"):
            with self.subTest(name=name):
                directory = self.root / name
                directory.mkdir()
                sentinel = directory / "demo-journal.json"
                sentinel.write_text('existing-state', encoding="utf-8")
                with patch("okx_bot.backtest.fetch_dataset") as fetch:
                    code, _, error = self.invoke(["--state-dir", str(directory)])
                self.assertEqual(code, 1, error)
                fetch.assert_not_called()
                self.assertEqual(sentinel.read_text(encoding="utf-8"), 'existing-state')
                self.assertEqual(list(directory.iterdir()), [sentinel])

    def test_network_failure_is_not_a_zero_result_and_does_not_echo_proxy(self):
        private_text = "http://example-user:do-not-show@proxy.invalid:8080"
        with patch("okx_bot.backtest.fetch_dataset", side_effect=RuntimeError(private_text)):
            code, output, error = self.invoke()
        self.assertEqual(code, 1)
        self.assertNotIn(private_text, output + error)
        self.assertNotIn("回测完成。", output)
        self.assertFalse((self.state / "report.json").exists())
        self.assertFalse((self.state / "report.html").exists())


if __name__ == "__main__":
    unittest.main()
