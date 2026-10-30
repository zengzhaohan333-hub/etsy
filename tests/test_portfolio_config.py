import tempfile
import unittest
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path

from okx_bot.config import Config, load_config
from okx_bot.portfolio_config import DEFAULT_INSTRUMENTS, PortfolioConfig, load_portfolio_config


class PortfolioConfigTests(unittest.TestCase):
    def load_text(self, text):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "portfolio.toml"
            path.write_text(text, encoding="utf-8")
            return load_portfolio_config(path)

    def test_user_profile_defaults_and_example_file_agree(self):
        profile = PortfolioConfig()
        self.assertEqual(load_portfolio_config(Path("config.portfolio.toml")), profile)
        self.assertEqual(profile.instruments, DEFAULT_INSTRUMENTS)
        self.assertEqual(profile.bar, "5m")
        self.assertEqual(profile.max_notional, D("200"))
        self.assertEqual(profile.max_total_notional, D("800"))

    def test_market_configs_are_independent_configs_with_shared_strategy_settings(self):
        profile = PortfolioConfig()
        markets = profile.market_configs()
        self.assertIsInstance(markets, tuple)
        self.assertEqual(tuple(config.instrument for config in markets), DEFAULT_INSTRUMENTS)
        self.assertEqual(len({id(config) for config in markets}), 4)
        for market in markets:
            with self.subTest(instrument=market.instrument):
                self.assertIsInstance(market, Config)
                self.assertEqual(market.bar, "5m")
                self.assertEqual(market.max_notional, D("200"))
                self.assertEqual(market.risk_per_trade, D("0.005"))
                self.assertEqual(market.stop_loss_pct, D("0.01"))
                self.assertEqual(market.take_profit_pct, D("0.02"))
                self.assertEqual(market.max_drawdown_pct, D("0.05"))

    def test_overrides_propagate_to_all_markets_and_preserve_declared_order(self):
        order = tuple(reversed(DEFAULT_INSTRUMENTS))
        profile = replace(PortfolioConfig(), instruments=order, max_notional=D("50"), max_total_notional=D("200"),
                          fast_window=3, slow_window=15, initial_equity=D("5000"), stop_loss_pct=D("0.02"))
        self.assertEqual(tuple(config.instrument for config in profile.market_configs()), order)
        for market in profile.market_configs():
            self.assertEqual(market.max_notional, D("50"))
            self.assertEqual(market.initial_equity, D("5000"))
            self.assertEqual(market.stop_loss_pct, D("0.02"))
            self.assertEqual((market.fast_window, market.slow_window), (3, 15))

    def test_instrument_set_is_complete_unique_and_allowlisted(self):
        invalid = [(), DEFAULT_INSTRUMENTS[:-1], DEFAULT_INSTRUMENTS + ("DOGE-USDT-SWAP",),
                   ("BTC-USDT-SWAP", "BTC-USDT-SWAP", "SOL-USDT-SWAP", "XRP-USDT-SWAP"),
                   ("BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "DOGE-USDT-SWAP"),
                   list(DEFAULT_INSTRUMENTS), "BTC-USDT-SWAP", ([], *DEFAULT_INSTRUMENTS[1:])]
        for value in invalid:
            with self.subTest(value_type=type(value).__name__), self.assertRaises(ValueError):
                replace(PortfolioConfig(), instruments=value)

    def test_fixed_bar_and_existing_config_risk_constraints_are_enforced(self):
        for changes in ({"bar": "15m"}, {"bar": ["5m"]}, {"fast_window": True}, {"slow_window": 201},
                        {"fast_window": 20, "slow_window": 5}, {"risk_per_trade": D("0.03")},
                        {"max_position_fraction": D("1.1")}, {"max_notional": D("0")},
                        {"initial_equity": D("NaN")}, {"fee_rate": D("-0.001")},
                        {"slippage_rate": D("Infinity")}, {"stop_loss_pct": D("0")},
                        {"take_profit_pct": D("1")}, {"max_drawdown_pct": D("0.8")}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(PortfolioConfig(), **changes)

    def test_shared_cap_must_be_finite_positive_decimal_and_at_least_per_market(self):
        for total in (D("0"), D("-1"), D("199.99"), D("NaN"), D("Infinity"), 800, 800.0, True, "800"):
            with self.subTest(total_type=type(total).__name__), self.assertRaises(ValueError):
                replace(PortfolioConfig(), max_total_notional=total)
        self.assertEqual(replace(PortfolioConfig(), max_total_notional=D("200")).max_total_notional, D("200"))

    def test_toml_numeric_inputs_become_decimals_and_market_array_becomes_tuple(self):
        profile = self.load_text('max_notional = 100\nmax_total_notional = "400"\nrisk_per_trade = 0.005\n')
        self.assertEqual(profile.max_notional, D("100"))
        self.assertEqual(profile.max_total_notional, D("400"))
        self.assertEqual(profile.risk_per_trade, D("0.005"))
        profile = self.load_text('instruments = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "SOL-USDT-SWAP", "XRP-USDT-SWAP"]\n')
        self.assertIsInstance(profile.instruments, tuple)

    def test_unknown_fields_booleans_arrays_and_malformed_toml_are_rejected(self):
        for text in ('live = true\n', 'api_key = "example-key-must-not-print"\n',
                     'max_total_notional = true\n', 'risk_per_trade = []\n',
                     'max_notional = "NaN"\n', 'max_total_notional = inf\n',
                     'max_total_notional = "not-a-number"\n', 'bar = 5\n',
                     'instruments = "BTC-USDT-SWAP"\n', 'instruments = [1,2,3,4]\n',
                     'max_total_notional = 800\nmax_total_notional = 900\n'):
            with self.subTest(text=text), self.assertRaises(ValueError) as raised:
                self.load_text(text)
            self.assertNotIn("example-key-must-not-print", str(raised.exception))

    def test_old_single_market_profile_is_unchanged(self):
        single = load_config(Path("config.paper.toml"))
        self.assertEqual(single, Config())
        self.assertEqual(single.bar, "15m")
        self.assertEqual(single.max_notional, D("200"))


if __name__ == "__main__":
    unittest.main()
