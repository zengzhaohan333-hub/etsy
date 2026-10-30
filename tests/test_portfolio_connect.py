import contextlib
import io
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock, patch

from okx_bot import connect
from okx_bot.okx import Credentials, Instrument, OkxError
from okx_bot.portfolio_config import PortfolioConfig


class PublicAndDemoClient:
    def __init__(self):
        self.credentials = Credentials("example-key", "example-secret", "example-passphrase")
        self.server_time = Mock(side_effect=lambda: time.time_ns() // 1_000_000)
        self.account_config = Mock(return_value={"posMode": "net_mode", "acctLv": "2"})
        self.balances = Mock(return_value={"currency": "USDT", "equity": Decimal("5000"),
                                          "available": Decimal("5000")})
        self.instrument = Mock(side_effect=lambda name: Instrument(name, Decimal("0.01"),
            Decimal("0.01"), Decimal("0.01"), Decimal("100000"), ct_val_ccy=name.split("-")[0]))
        self.positions = Mock(return_value=[])
        self.pending_orders = Mock(return_value=[])
        self.pending_algo_orders = Mock(return_value=[])


class PortfolioConnectTests(unittest.TestCase):
    def setUp(self):
        self.config = PortfolioConfig()
        self.client = PublicAndDemoClient()

    def test_four_market_check_counts_shared_equity_once_without_mutation(self):
        result = connect.check_portfolio(self.config, self.client)
        self.assertEqual(result["equity"], Decimal("5000"))
        self.assertEqual(result["instruments"], self.config.instruments)
        self.client.account_config.assert_called_once_with()
        self.client.balances.assert_called_once_with()
        self.client.server_time.assert_called_once_with()
        for method in (self.client.instrument, self.client.positions,
                       self.client.pending_orders, self.client.pending_algo_orders):
            self.assertEqual([call.args for call in method.call_args_list],
                             [(name,) for name in self.config.instruments])

    def test_existing_position_or_orders_in_any_market_rejects_whole_group(self):
        for name in self.config.instruments:
            for field in ("positions", "pending_orders", "pending_algo_orders"):
                with self.subTest(instrument=name, field=field):
                    client = PublicAndDemoClient()
                    result = [{"pos": "1"}] if field == "positions" else [{"id": "existing"}]
                    getattr(client, field).side_effect = lambda instrument: result if instrument == name else []
                    with self.assertRaises(connect.ConnectionCheckError) as raised:
                        connect.check_portfolio(self.config, client)
                    self.assertIn(name, str(raised.exception))

    def test_bad_public_clock_prevents_any_private_request(self):
        self.client.server_time.return_value = 0
        self.client.server_time.side_effect = None
        with self.assertRaises(connect.ConnectionCheckError):
            connect.check_portfolio(self.config, self.client)
        self.client.account_config.assert_not_called()

    def test_bad_account_mode_or_nonpositive_balance_rejects_group(self):
        for change in ("mode", "balance"):
            client = PublicAndDemoClient()
            if change == "mode":
                client.account_config.return_value = {"posMode": "long_short_mode", "acctLv": "2"}
            else:
                client.balances.return_value["available"] = Decimal("0")
            with self.subTest(change=change), self.assertRaises(connect.ConnectionCheckError):
                connect.check_portfolio(self.config, client)
            client.instrument.assert_not_called()

    def invoke_wizard(self, extra=(), answer="", *, result=None):
        output, errors = io.StringIO(), io.StringIO()
        client = self.client
        runner = Mock(return_value=result or {"mode": "demo", "halted": False})
        confirm = Mock(return_value=answer)
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / ".runtime-demo"
            state.mkdir()
            journal = state / "demo-journal.json"
            original = '{"version":1,"orders":[{"status":"closed","marker":"keep"}]}'
            journal.write_text(original)
            with patch("okx_bot.connect.collect_credentials", return_value=client.credentials), \
                 patch("okx_bot.connect.OkxClient", return_value=client), \
                 patch("okx_bot.connect.load_portfolio_config", return_value=self.config) as loader, \
                 patch("okx_bot.connect._is_tty", return_value=True), \
                 patch("okx_bot.portfolio.run_portfolio", runner), \
                 patch("okx_bot.connect.run_demo") as single_runner, \
                 patch("builtins.input", confirm), \
                 contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                code = connect.main(["--portfolio", "--state-dir", str(state), *extra])
            self.assertEqual(journal.read_text(), original)
            single_runner.assert_not_called()
        return code, output.getvalue(), errors.getvalue(), runner, confirm, loader

    def test_check_only_has_four_markets_and_no_confirmation_or_runner(self):
        code, output, _, runner, confirm, loader = self.invoke_wizard(["--check-only"])
        self.assertEqual(code, 0)
        loader.assert_called_once_with(Path("config.portfolio.toml"))
        runner.assert_not_called()
        confirm.assert_not_called()
        self.assertIn("800", output)
        self.assertIn("5m", output)
        for name in self.config.instruments:
            self.assertIn(name, output)

    def test_exact_demo_confirmation_starts_one_shared_coordinator(self):
        code, output, _, runner, confirm, _ = self.invoke_wizard(answer="DEMO")
        self.assertEqual(code, 0)
        runner.assert_called_once()
        self.assertIs(runner.call_args.args[0], self.config)
        self.assertIs(runner.call_args.kwargs["client"], self.client)
        confirm.assert_called_once()
        self.assertIn("账户权益与会话回撤只计算一次", output)

    def test_empty_or_inexact_confirmation_never_starts_coordinator(self):
        for answer in ("", "demo", " DEMO", "DEMO "):
            with self.subTest(answer=answer):
                code, _, _, runner, _, _ = self.invoke_wizard(answer=answer)
                self.assertEqual(code, 0)
                runner.assert_not_called()

    def test_private_check_failure_cannot_start_and_does_not_echo_secret(self):
        self.client.account_config.side_effect = OkxError("example-secret", api_code="50110", http_status=401)
        code, output, errors, runner, confirm, _ = self.invoke_wizard(answer="DEMO")
        self.assertEqual(code, 1)
        self.assertIn("50110", errors)
        self.assertNotIn("example-secret", output + errors)
        runner.assert_not_called()
        confirm.assert_not_called()

    def test_unresolved_journal_refusal_names_the_safe_recovery_entry(self):
        result = {"halted": True, "halt_reason":
            "Unresolved or invalid demo journal; manually reconcile exchange positions, orders and algos before recovery"}
        code, _, errors, runner, _, _ = self.invoke_wizard(answer="DEMO", result=result)
        self.assertEqual(code, 1)
        runner.assert_called_once()
        self.assertIn("reconcile-okx-portfolio.bat", errors)
        self.assertIn("保留原日志", errors)

    def test_execution_service_error_explains_code_without_echoing_halt_reason(self):
        result = {"halted": True, "halt_reason": "example-secret", "halt_api_code": "50001",
                  "halt_category": "api", "halt_http_status": 200}
        code, output, errors, _, _, _ = self.invoke_wizard(answer="DEMO", result=result)
        self.assertEqual(code, 1)
        self.assertIn("50001", errors)
        self.assertIn("维护", errors)
        self.assertNotIn("example-secret", output + errors)

    def test_reconciliation_never_runs_trading_or_asks_for_demo_confirmation(self):
        for halted in (False, True):
            with self.subTest(halted=halted), patch("okx_bot.portfolio.reconcile_portfolio",
                return_value={"halted": halted}) as reconcile:
                code, output, _, runner, confirm, _ = self.invoke_wizard(["--reconcile"], answer="DEMO")
                self.assertEqual(code, int(halted))
                reconcile.assert_called_once()
                self.client.server_time.assert_not_called()
                runner.assert_not_called()
                confirm.assert_not_called()
                self.assertIn("不会下单、撤单或设置杠杆", output)

    def test_invalid_reconcile_flags_are_rejected_before_credential_collection(self):
        for args in (["--reconcile"], ["--portfolio", "--reconcile", "--check-only"]):
            with self.subTest(args=args), patch("okx_bot.connect.collect_credentials") as collect, \
                 contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(connect.main(args), 1)
                collect.assert_not_called()


if __name__ == "__main__":
    unittest.main()
