"""Offline experiment wiring; generated fixtures do not establish performance."""
import contextlib
from decimal import Decimal as D
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from okx_bot import research_low_turnover as experiment
from okx_bot.history import HistoryError, save_dataset
from okx_bot.portfolio_config import DEFAULT_INSTRUMENTS, PortfolioConfig
from test_research import dataset, engine_stub


class LowTurnoverResearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.datasets = {symbol: dataset(symbol) for symbol in DEFAULT_INSTRUMENTS}

    def test_three_fixed_200u_cases_share_data_capital_and_actual_cost_scenarios(self):
        profile = PortfolioConfig()
        with patch.object(experiment, "run_portfolio_backtest", side_effect=engine_stub) as engine:
            report = experiment.build_low_turnover_report(profile, self.datasets, capital=D("5000"))
        self.assertEqual(engine.call_count, 12)
        self.assertEqual([case["strategy"] for case in report["scenarios"]],
                         ["sma_cross", "trend_breakout_atr", "low_turnover_v2"])
        self.assertEqual(profile.max_total_notional, D("800"))
        self.assertEqual(report["profile"]["max_notional"], "200")
        self.assertEqual(report["profile"]["max_total_notional"], "1000")
        start, end = next(iter(self.datasets.values())).start_ms, next(iter(self.datasets.values())).end_ms
        split = report["split"]["train_end_ms"]
        for index, call in enumerate(engine.call_args_list):
            self.assertIs(call.args[1], self.datasets)
            self.assertEqual(call.args[0].max_notional, D("200"))
            self.assertEqual(call.args[0].max_total_notional, D("1000"))
            self.assertEqual(call.kwargs["starting_equity"], D("5000"))
            self.assertEqual((call.kwargs["start_ms"], call.kwargs["end_ms"]),
                ((start, end), (start, split), (split, end), (split, end))[index % 4])
            self.assertEqual(call.kwargs["fee_multiplier"], D(2) if index % 4 == 3 else D(1))
            self.assertEqual(call.kwargs["slippage_multiplier"], D(2) if index % 4 == 3 else D(1))
        validation = report["validation"]
        self.assertEqual(validation["status"], "development_seen")
        self.assertTrue(validation["unseen_validation_required"])
        self.assertFalse(validation["deployment_ready"])
        self.assertEqual(validation["frozen_at_ms"], experiment.RULES_FROZEN_AT_MS)
        self.assertEqual(len(validation["rules_sha256"]), 64)
        self.assertIn("后30%不再是独立样本外验证", validation["label"])

    def test_daily_profit_target_does_not_change_rules_caps_or_intervals(self):
        calls = []
        for target in (D("200"), D("1000")):
            with patch.object(experiment, "run_portfolio_backtest", side_effect=engine_stub) as engine:
                report = experiment.build_low_turnover_report(PortfolioConfig(), self.datasets,
                                                              capital=D("5000"), daily_target=target)
            calls.append([(call.args, {key: value for key, value in call.kwargs.items() if key != "daily_target"})
                          for call in engine.call_args_list])
            self.assertEqual(report["daily_target"], str(target))
        self.assertEqual(calls[0], calls[1])

    def test_missing_misaligned_or_out_of_range_data_cannot_reach_engine(self):
        for defect in ("missing", "time", "short"):
            data = dict(self.datasets)
            if defect == "missing":
                data.pop(DEFAULT_INSTRUMENTS[0])
            elif defect == "time":
                data[DEFAULT_INSTRUMENTS[-1]] = dataset(DEFAULT_INSTRUMENTS[-1], days=8)
            else:
                data = {symbol: dataset(symbol, days=1) for symbol in DEFAULT_INSTRUMENTS}
            with self.subTest(defect=defect), patch.object(experiment, "run_portfolio_backtest") as engine:
                with self.assertRaises((experiment.ResearchError, HistoryError)):
                    experiment.build_low_turnover_report(PortfolioConfig(), data, capital=D("5000"))
                engine.assert_not_called()

    def invoke(self, source, target):
        output = io.StringIO()
        with contextlib.redirect_stdout(output), contextlib.redirect_stderr(output):
            code = experiment.main(["--dataset-dir", str(source), "--state-dir", str(target)])
        return code, output.getvalue()

    def test_cli_reuses_source_files_without_network_keys_or_live_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, target = base / "previous-data", base / "new-development"
            source.mkdir()
            for name, value in self.datasets.items():
                save_dataset(value, source / (name + ".json"))
            before = {path.name: path.read_bytes() for path in source.iterdir()}
            live = base / ".runtime-demo"
            live.mkdir()
            journal = live / "demo-journal.json"
            journal.write_text('{"orders":[{"status":"protected"}]}')
            live_before = journal.read_bytes()
            with patch.object(experiment, "load_portfolio_config", return_value=PortfolioConfig()), \
                 patch("okx_bot.history.PublicHistoryClient", side_effect=AssertionError("no network")), \
                 patch("okx_bot.okx.Credentials.from_env", side_effect=AssertionError("no credentials")), \
                 patch.object(experiment, "run_portfolio_backtest", side_effect=engine_stub) as engine:
                code, _ = self.invoke(source, target)
            self.assertEqual(code, 0)
            self.assertEqual(engine.call_count, 12)
            self.assertEqual(before, {path.name: path.read_bytes() for path in source.iterdir()})
            self.assertEqual(journal.read_bytes(), live_before)
            summary = json.loads((target / "summary.json").read_text())
            self.assertEqual(summary["validation"]["status"], "development_seen")
            self.assertEqual(set(summary["source"]["datasets"]), set(DEFAULT_INSTRUMENTS))
            html = (target / "report.html").read_text()
            self.assertIn("开发复盘", html)
            self.assertIn("后30%不再是独立样本外验证", html)
            self.assertNotIn("规则在读取结果前固定", html)

    def test_old_results_and_live_state_cannot_be_output_or_overwritten(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source = base / "old-research" / "datasets"
            source.mkdir(parents=True)
            for target in (source, source / "results", source.parent, base / ".runtime-demo"):
                with self.subTest(target=target), patch.object(experiment, "load_portfolio_config") as config:
                    code, _ = self.invoke(source, target)
                    self.assertEqual(code, 1)
                    config.assert_not_called()
                    self.assertFalse((target / "report.json").exists())

    def test_unexpected_raw_exception_is_not_printed_as_diagnostic(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            with patch.object(experiment, "load_portfolio_config", side_effect=RuntimeError("private-sentinel")):
                code, message = self.invoke(base / "source", base / "result")
            self.assertEqual(code, 1)
            self.assertNotIn("private-sentinel", message)


if __name__ == "__main__":
    unittest.main()
