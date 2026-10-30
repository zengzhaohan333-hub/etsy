import contextlib
from decimal import Decimal
import io
import json
from pathlib import Path
import signal
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from okx_bot import service
from okx_bot.okx import Credentials, OkxError
from okx_bot.portfolio_config import PortfolioConfig
from okx_bot.demo import DemoHalt
from okx_bot.session import PortfolioSession, SessionError


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.credentials = Credentials("test-key", "test-secret", "test-passphrase")
        self.profile = PortfolioConfig()
        self.client = object()
        self.output = io.StringIO()
        self.runner = Mock(return_value={"halted": False, "phase": "stopped"})
        self.reconcile = Mock(return_value={"halted": False, "reconciled": True, "closed": 1})
        self.preflight = Mock(return_value={"equity": Decimal("5000")})
        self.loader = Mock(return_value=self.credentials)
        self.factory = Mock(return_value=self.client)
        patches = [patch("okx_bot.service.load_portfolio_config", return_value=self.profile),
                   patch("okx_bot.service.load_service_credentials", self.loader),
                   patch("okx_bot.service.OkxClient", self.factory),
                   patch("okx_bot.service.run_portfolio", self.runner),
                   patch("okx_bot.service.reconcile_portfolio", self.reconcile),
                   patch("okx_bot.service.check_portfolio", self.preflight),
                   patch("builtins.input", side_effect=AssertionError("service cannot prompt")),
                   patch("getpass.getpass", side_effect=AssertionError("service cannot prompt")),
                   contextlib.redirect_stdout(self.output)]
        for item in patches:
            item.__enter__()
            self.addCleanup(item.__exit__, None, None, None)

    def events(self):
        return [json.loads(line) for line in self.output.getvalue().splitlines()]

    def assert_no_secrets(self):
        for secret in ("test-key", "test-secret", "test-passphrase"):
            self.assertNotIn(secret, self.output.getvalue())

    def test_service_refuses_before_credentials_without_explicit_demo_ack(self):
        self.assertEqual(service.main([]), service.EXIT_BLOCKED)
        self.loader.assert_not_called()
        self.factory.assert_not_called()
        self.runner.assert_not_called()
        self.assertEqual(self.events()[0]["reason"], "explicit_demo_ack_required")

    def test_noninteractive_start_enables_in_process_recovery_and_verified_resume(self):
        old = signal.getsignal(signal.SIGTERM)
        self.assertEqual(service.main(["--ack-demo-orders"]), 0)
        self.loader.assert_called_once_with(None)
        self.factory.assert_called_once_with(credentials=self.credentials)
        self.runner.assert_called_once()
        self.assertIs(self.runner.call_args.args[0], self.profile)
        self.assertEqual(self.runner.call_args.args[1], Path("/var/lib/okxbot"))
        self.assertEqual(self.runner.call_args.kwargs,
                         {"client": self.client, "poll_seconds": 15, "resilient": True, "resume": True})
        self.assertEqual(signal.getsignal(signal.SIGTERM), old)
        self.assertEqual([e["event"] for e in self.events()], ["service_starting", "service_stopped"])
        self.preflight.assert_not_called()  # Flat-only deployment check must not block an owned protected restart.
        self.assert_no_secrets()

    def test_signal_stops_controller_and_restores_handler(self):
        previous = signal.getsignal(signal.SIGTERM)
        def run(_config, _state, stop, **_kwargs):
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            self.assertTrue(stop.is_set())
            return {"halted": False}
        self.runner.side_effect = run
        self.assertEqual(service.main(["--ack-demo-orders"]), 0)
        self.assertEqual(signal.getsignal(signal.SIGTERM), previous)

    def test_safety_halt_has_nonrestartable_exit_without_raw_reason(self):
        self.runner.return_value = {"halted": True, "halt_reason": "test-secret", "halt_category": "api",
                                    "halt_api_code": "50001", "halt_http_status": 200}
        self.assertEqual(service.main(["--ack-demo-orders"]), service.EXIT_BLOCKED)
        self.assertEqual(self.events()[-1]["api_code"], "50001")
        self.assertEqual(self.events()[-1]["reason"], "account_or_journal_review_required")
        self.assert_no_secrets()

    def test_untrusted_diagnostics_are_not_logged(self):
        self.runner.return_value = {"halted": True, "halt_reason": "test-secret",
            "halt_api_code": "50001 test-key", "halt_category": ["test-secret"],
            "halt_http_status": "test-passphrase", "halt_tls_failure": "test-secret"}
        self.assertEqual(service.main(["--ack-demo-orders"]), service.EXIT_BLOCKED)
        self.assertEqual(set(self.events()[-1]), {"event", "reason"})
        self.assert_no_secrets()

    def test_read_only_check_does_not_create_state_or_start_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary) / "does-not-exist"
            self.assertEqual(service.main(["--check-only", "--state-dir", str(state)]), 0)
            self.assertFalse(state.exists())
        self.preflight.assert_called_once_with(self.profile, self.client)
        self.runner.assert_not_called()
        self.reconcile.assert_not_called()

    def test_reconciliation_requires_explicit_ack_and_never_starts_trading(self):
        self.assertEqual(service.main(["--reconcile"]), service.EXIT_BLOCKED)
        self.reconcile.assert_not_called()
        self.loader.assert_not_called()
        self.assertEqual(service.main(["--reconcile", "--ack-journal"]), 0)
        self.reconcile.assert_called_once_with(self.profile, Path("/var/lib/okxbot"), client=self.client)
        self.runner.assert_not_called()
        self.preflight.assert_not_called()

    def test_reconciliation_failure_keeps_safety_exit_and_cannot_start_service(self):
        self.reconcile.return_value = {"halted": True, "halt_reason": "test-secret"}
        self.assertEqual(service.main(["--reconcile", "--ack-journal"]), service.EXIT_BLOCKED)
        self.runner.assert_not_called()
        self.assert_no_secrets()

    def test_session_initialization_requires_ack_and_is_not_a_trading_start(self):
        self.assertEqual(service.main(["--initialize-session"]), service.EXIT_BLOCKED)
        self.loader.assert_not_called()
        with patch("okx_bot.service.initialize_session") as initialize:
            self.assertEqual(service.main(["--initialize-session", "--ack-session-init"]), 0)
        initialize.assert_called_once_with(self.profile, Path("/var/lib/okxbot"), self.client)
        self.loader.assert_called_once_with(None)
        self.runner.assert_not_called()
        self.reconcile.assert_not_called()
        self.assertFalse(self.events()[-1]["trading_started"])

    def test_explicit_initializer_checks_twice_and_keeps_latched_session(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            client = SimpleNamespace(credentials=self.credentials,
                balances=Mock(return_value={"currency": "USDT", "equity": Decimal("5000")}),
                get_leverage=Mock(side_effect=lambda instrument: [
                    {"instId": instrument, "mgnMode": "isolated", "lever": "1.00"}]))
            initialized = service.initialize_session(self.profile, state, client)
            self.assertEqual(self.preflight.call_count, 2)
            self.assertTrue((state / "session.json").exists())
            self.assertTrue((state / "demo-journal.json").exists())
            self.assertTrue(initialized.observe(Decimal("4750"), Decimal(".05")))
            before = (state / "session.json").read_bytes()
            with self.assertRaises(SessionError):
                service.initialize_session(self.profile, state, client)
            self.assertEqual((state / "session.json").read_bytes(), before)

    def test_initializer_requires_verified_leverage_without_writing_exchange_settings(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            client = SimpleNamespace(credentials=self.credentials,
                get_leverage=Mock(return_value=[{"instId": "BTC-USDT-SWAP", "mgnMode": "isolated", "lever": "2"}]))
            with self.assertRaises(SessionError):
                service.initialize_session(self.profile, state, client)
            self.assertFalse((state / "session.json").exists())

    def test_initializer_refuses_unresolved_order_journal_before_queries(self):
        with tempfile.TemporaryDirectory() as temporary:
            state = Path(temporary)
            (state / "demo-journal.json").write_text('{"version":1,"orders":[{"status":"intent"}]}')
            with self.assertRaises(DemoHalt):
                service.initialize_session(self.profile, state, SimpleNamespace(credentials=self.credentials))
            self.preflight.assert_not_called()
            self.assertFalse((state / "session.json").exists())

    def test_known_local_setup_failure_is_explained_without_raw_exception_output(self):
        with patch("okx_bot.service.initialize_session", side_effect=SessionError(
            "All four demo instruments must already use isolated 1x leverage before service initialization")):
            self.assertEqual(service.main(["--initialize-session", "--ack-session-init"]), service.EXIT_BLOCKED)
        self.assertEqual(self.events()[-1]["reason"], "configure_four_markets_isolated_1x_first")
        self.assert_no_secrets()

    def test_journal_ack_cannot_be_misapplied_to_trading(self):
        self.assertEqual(service.main(["--ack-journal", "--ack-demo-orders"]), service.EXIT_BLOCKED)
        self.loader.assert_not_called()

    def test_credential_and_initialization_errors_never_echo_exception_text(self):
        self.loader.side_effect = ValueError("test-secret")
        self.assertEqual(service.main(["--ack-demo-orders"]), service.EXIT_BLOCKED)
        self.loader.side_effect = RuntimeError("test-passphrase")
        self.assertEqual(service.main(["--ack-demo-orders"]), service.EXIT_BUG)
        self.runner.assert_not_called()
        self.assert_no_secrets()

    def test_adapter_failure_is_not_automatically_treated_as_restartable(self):
        self.runner.side_effect = OkxError("test-secret", category="server", api_code="50001", http_status=503)
        self.assertEqual(service.main(["--ack-demo-orders"]), service.EXIT_BLOCKED)
        self.assert_no_secrets()

    def test_unexpected_controller_exception_does_not_produce_raw_traceback(self):
        self.runner.side_effect = RuntimeError("test-secret")
        self.assertEqual(service.main(["--ack-demo-orders"]), service.EXIT_BUG)
        self.assert_no_secrets()
        self.assertNotIn("Traceback", self.output.getvalue())


if __name__ == "__main__":
    unittest.main()
