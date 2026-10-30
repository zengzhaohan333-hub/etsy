"""Public-data orchestration fixtures, never historical performance evidence."""
import contextlib
from dataclasses import replace
from decimal import Decimal as D
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from okx_bot import research
from okx_bot.history import DAY_MS, HistoryDataset, HistoryError, save_dataset
from okx_bot.okx import Candle, Instrument
from okx_bot.portfolio_config import DEFAULT_INSTRUMENTS, PortfolioConfig


def dataset(instrument, days=7):
    interval = 300_000
    start = 5 * DAY_MS - 28_800_000  # A UTC+8 midnight, after 60h of warm-up.
    end = start + days * DAY_MS
    first = start - research.WARMUP_BARS * interval
    bars = [Candle(ts, D("100"), D("101"), D("99"), D("100"))
            for ts in range(first, end, interval)]
    metadata = Instrument(instrument, D("1"), D(".01"), D(".01"), D("1000000"),
                          D(".01"), instrument.split("-")[0])
    funding = [(ts, D(".0001")) for ts in range(start, end, 28_800_000)]
    return HistoryDataset(bars, list(bars), funding, metadata, start, end, end + interval,
                          "5m", research.WARMUP_BARS, (start - 28_800_000, D(".0001")),
                          (end, D(".0001")))


def engine_stub(_profile, _datasets, **options):
    return {"net_pnl": "0", "return_pct": "0", "max_drawdown_pct": "0", "entries": 0,
            "closed_trades": 0, "fees": "0", "funding_pnl": "0", "open_positions": {},
            "equity_curve": [], "daily_pnl": [], "daily_stats": {}, "trades": [],
            "start_ms": options["start_ms"], "end_ms": options["end_ms"], "halted": False}


class ResearchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.datasets = {name: dataset(name) for name in DEFAULT_INSTRUMENTS}

    def test_fixed_four_cases_share_capital_cap_data_and_chronological_split(self):
        profile = PortfolioConfig()
        before = profile
        with patch("okx_bot.research.run_portfolio_backtest", side_effect=engine_stub) as engine:
            report = research.build_research_report(profile, self.datasets, capital=D("5000"))
        self.assertEqual(profile, before)
        self.assertEqual(engine.call_count, 16)
        self.assertEqual([scenario["strategy"] for scenario in report["scenarios"]],
                         ["sma_cross", "sma_cross", "trend_breakout_atr", "trend_breakout_atr"])
        self.assertEqual([scenario["max_notional"] for scenario in report["scenarios"]], ["200", "500", "200", "500"])
        start, end = next(iter(self.datasets.values())).start_ms, next(iter(self.datasets.values())).end_ms
        split = report["split"]["train_end_ms"]
        for index, call in enumerate(engine.call_args_list):
            settings = call.args[0]
            self.assertIs(call.args[1], self.datasets)
            self.assertEqual(settings.max_total_notional, D("1000"))
            self.assertEqual(settings.max_notional, D("200") if index // 4 in (0, 2) else D("500"))
            self.assertEqual(call.kwargs["starting_equity"], D("5000"))
            self.assertEqual((call.kwargs["start_ms"], call.kwargs["end_ms"]),
                             ((start, end), (start, split), (split, end), (split, end))[index % 4])
            costs = D(2) if index % 4 == 3 else D(1)
            self.assertEqual(call.kwargs["fee_multiplier"], costs)
            self.assertEqual(call.kwargs["slippage_multiplier"], costs)
        self.assertEqual(report["profile"]["max_notional"], "500")
        self.assertIn("不是未来概率", "".join(report["warnings"]))

    def test_shareable_summary_keeps_daily_risk_and_open_positions_without_large_series(self):
        result = {"net_pnl": "-5", "daily_stats": {"target_days": 0},
                  "daily_pnl": [{"date": "2026-01-01", "net_pnl": "-5", "complete": True}],
                  "open_positions": {"ETH-USDT-SWAP": {"side": "short"}},
                  "equity_curve": [{"ts": 1, "equity": "5000"}], "trades": [{"net_pnl": "-5"}]}
        report = {"capital": "5000", "scenarios": [{"id": "candidate_500", "results": {"test": result}}]}
        summary = research.summarize_report(report)
        reduced = summary["scenarios"][0]["results"]["test"]
        self.assertNotIn("equity_curve", reduced)
        self.assertNotIn("trades", reduced)
        for name in ("net_pnl", "daily_stats", "daily_pnl", "open_positions"):
            self.assertEqual(reduced[name], result[name])
        self.assertIn("equity_curve", report["scenarios"][0]["results"]["test"])

    def test_mismatched_or_short_warmup_cannot_reach_engine(self):
        for change in ("missing", "boundaries", "warmup"):
            data = dict(self.datasets)
            if change == "missing":
                data.pop(DEFAULT_INSTRUMENTS[-1])
            elif change == "boundaries":
                data[DEFAULT_INSTRUMENTS[-1]] = dataset(DEFAULT_INSTRUMENTS[-1], days=8)
            else:
                data[DEFAULT_INSTRUMENTS[-1]] = replace(data[DEFAULT_INSTRUMENTS[-1]], warmup_bars=21)
            with self.subTest(change=change), patch("okx_bot.research.run_portfolio_backtest") as engine:
                with self.assertRaises((research.ResearchError, HistoryError)):
                    research.build_research_report(PortfolioConfig(), data, capital=D("5000"))
                engine.assert_not_called()

    def invoke(self, state, *arguments):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return research.main(["--days", "7", "--state-dir", str(state), *arguments])

    def test_online_partial_download_reuses_completed_coins_and_fixed_anchor_on_rerun(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / ".runtime-research"
            settings = PortfolioConfig()
            end = self.datasets[DEFAULT_INSTRUMENTS[0]].end_ms
            calls = []
            fail = {"enabled": True}
            def fetch(config, **options):
                calls.append((config.instrument, options))
                if config.instrument == DEFAULT_INSTRUMENTS[2] and fail["enabled"]:
                    raise HistoryError("temporary public failure")
                return self.datasets[config.instrument]
            with patch("okx_bot.research.time.time_ns", return_value=end * 1_000_000), \
                 patch("okx_bot.research.load_portfolio_config", return_value=settings), \
                 patch("okx_bot.research.fetch_dataset", side_effect=fetch), \
                 patch("okx_bot.research.build_research_report", return_value={"mode": "research"}) as build, \
                 patch("okx_bot.research.render_research_report", return_value="<!doctype html><p>research</p>"):
                self.assertEqual(self.invoke(state), 1)
                build.assert_not_called()
                self.assertTrue((state / "datasets" / (DEFAULT_INSTRUMENTS[0] + ".json")).exists())
                fail["enabled"] = False
                self.assertEqual(self.invoke(state), 0)
            self.assertEqual([name for name, _ in calls],
                             [*DEFAULT_INSTRUMENTS[:3], *DEFAULT_INSTRUMENTS[2:]])
            self.assertTrue(all(options["now_ms"] == end and options["warmup_bars"] == 720
                                for _, options in calls))
            self.assertTrue((state / "report.html").exists())

    def test_offline_run_has_no_keys_network_or_running_state_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            source, target = base / "public-data", base / "research"
            source.mkdir()
            for name, value in self.datasets.items():
                save_dataset(value, source / (name + ".json"))
            live = base / ".runtime-demo"
            live.mkdir()
            journal = live / "demo-journal.json"
            journal.write_text('{"version":1,"orders":[{"status":"protected"}]}')
            before = journal.read_bytes()
            with patch("okx_bot.research.load_portfolio_config", return_value=PortfolioConfig()), \
                 patch("okx_bot.research.fetch_dataset", side_effect=AssertionError("no network")), \
                 patch("okx_bot.okx.Credentials.from_env", side_effect=AssertionError("no keys")), \
                 patch("okx_bot.research.run_portfolio_backtest", side_effect=engine_stub) as engine:
                self.assertEqual(self.invoke(target, "--dataset-dir", str(source)), 0)
            self.assertEqual(engine.call_count, 16)
            self.assertEqual(journal.read_bytes(), before)
            report = json.loads((target / "report.json").read_text())
            self.assertEqual(report["capital"], "5000")
            self.assertEqual(report["daily_target"], "200")
            self.assertEqual(set(report["source"]["datasets"]), set(DEFAULT_INSTRUMENTS))
            self.assertTrue((target / "summary.json").exists())
            html = (target / "report.html").read_text()
            self.assertIn("非预测", html)

    def test_live_directory_or_manifest_parameter_mismatch_fails_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            base = Path(temporary)
            live = base / ".runtime-demo"
            live.mkdir()
            self.assertEqual(self.invoke(live), 1)
            custom_live = base / "custom-bot-state"
            custom_live.mkdir()
            status = custom_live / "status.json"
            status.write_text('{"mode":"demo","halted":false}')
            with patch("okx_bot.research.load_portfolio_config") as load_config:
                self.assertEqual(self.invoke(custom_live), 1)
                load_config.assert_not_called()
            self.assertEqual(status.read_text(), '{"mode":"demo","halted":false}')
            self.assertFalse((custom_live / "research.lock").exists())
            state = base / "research"
            state.mkdir()
            (state / "dataset_manifest.json").write_text('{"version":1,"days":30,"end_ms":300000,"warmup_bars":720}')
            with patch("okx_bot.research.load_portfolio_config", return_value=PortfolioConfig()), \
                 patch("okx_bot.research.fetch_dataset") as fetch:
                self.assertEqual(self.invoke(state), 1)
                fetch.assert_not_called()


if __name__ == "__main__":
    unittest.main()
