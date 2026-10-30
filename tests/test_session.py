import json
import os
from dataclasses import replace
from decimal import Decimal as D
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from okx_bot.portfolio_config import PortfolioConfig
from okx_bot.session import PortfolioSession, SessionError, profile_fingerprint


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.directory = Path(self.temporary.name)
        self.path = self.directory / "session.json"
        self.account = "a" * 64
        self.profile = profile_fingerprint(PortfolioConfig())

    def load(self, **changes):
        args = dict(state_dir=self.directory, api_fingerprint=self.account,
                    config_fingerprint=self.profile, initial_equity=D("5000"), allow_initialize=True)
        args.update(changes)
        return PortfolioSession.load_or_create(**args)

    def test_peak_and_drawdown_latch_survive_restart_and_equity_recovery(self):
        session = self.load()
        self.assertFalse(session.observe(D("5200"), D(".05")))
        self.assertFalse(session.observe(D("5100"), D(".05")))
        restarted = self.load(initial_equity=D("5100"))
        self.assertEqual(restarted.peak, D("5200"))
        self.assertTrue(restarted.observe(D("4940"), D(".05")))
        final = self.load(initial_equity=D("5500"))
        self.assertTrue(final.risk_halted)
        self.assertEqual(final.latched_reason, "session_drawdown")
        self.assertTrue(final.observe(D("5500"), D(".05")))

    def test_no_risk_change_does_not_rewrite_state_on_every_poll(self):
        session = self.load()
        original = self.path.read_bytes()
        with patch("okx_bot.session._write", side_effect=AssertionError("unnecessary rewrite")):
            self.assertFalse(session.observe(D("4999"), D(".05")))
        self.assertEqual(self.path.read_bytes(), original)

    def test_account_and_strategy_binding_changes_refuse_without_replacing_state(self):
        self.load()
        before = self.path.read_bytes()
        for changes in ({"api_fingerprint": "b" * 64}, {"config_fingerprint": "c" * 64}):
            with self.subTest(changes=changes), self.assertRaises(SessionError):
                self.load(**changes)
            self.assertEqual(self.path.read_bytes(), before)

    def test_missing_session_cannot_reset_peak_when_journal_is_active(self):
        journal = self.directory / "demo-journal.json"
        for status in ("protected", "accepted", "intent", "inspect_required", "unrecognized"):
            with self.subTest(status=status):
                journal.write_text(json.dumps({"version": 1, "orders": [{"status": status}]}))
                before = journal.read_bytes()
                with self.assertRaises(SessionError):
                    self.load()
                self.assertFalse(self.path.exists())
                self.assertEqual(journal.read_bytes(), before)

    def test_closed_legacy_journal_can_initialize_a_reviewed_service_session(self):
        (self.directory / "demo-journal.json").write_text(
            json.dumps({"version": 1, "orders": [{"status": "closed", "instrument": "ETH-USDT-SWAP"}]}))
        self.assertEqual(self.load().peak, D("5000"))
        self.assertEqual(os.stat(self.path).st_mode & 0o777, 0o600)

    def test_existing_session_cannot_silently_replace_a_missing_order_journal(self):
        self.load()
        journal = self.directory / "demo-journal.json"
        self.assertTrue(journal.exists())
        journal.unlink()
        before = self.path.read_bytes()
        with self.assertRaises(SessionError):
            self.load()
        self.assertFalse(journal.exists())
        self.assertEqual(self.path.read_bytes(), before)

    def test_normal_service_refuses_missing_risk_even_after_all_orders_closed(self):
        self.load()
        self.path.unlink()
        before = (self.directory / "demo-journal.json").read_bytes()
        with self.assertRaises(SessionError):
            self.load(allow_initialize=False)
        self.assertFalse(self.path.exists())
        self.assertEqual((self.directory / "demo-journal.json").read_bytes(), before)

    def test_normal_service_cannot_guess_that_empty_directory_is_a_new_session(self):
        with self.assertRaises(SessionError):
            self.load(allow_initialize=False)
        self.assertEqual(list(self.directory.iterdir()), [])

    def test_malformed_state_never_invents_new_peak_or_echoes_content(self):
        self.load()
        valid = json.loads(self.path.read_text())
        bodies = ["private-secret", '{"version":1,"version":1}', "NaN"]
        for key, value in (("version", True), ("peak_equity", "NaN"), ("peak_equity", "-1"),
                           ("peak_equity", "0"), ("risk_halted", "false"),
                           ("latched_reason", "private-secret"), ("extra", "private-secret"),
                           ("updated_at_ms", True)):
            data = dict(valid)
            data[key] = value
            bodies.append(json.dumps(data))
        for body in bodies:
            with self.subTest(body=body):
                self.path.write_text(body)
                with self.assertRaises(SessionError) as raised:
                    self.load()
                self.assertNotIn("private-secret", str(raised.exception))
                self.assertEqual(self.path.read_text(), body)

    def test_symlink_or_group_writable_state_is_refused(self):
        other = self.directory / "original.json"
        other.write_text("private-secret")
        self.path.symlink_to(other)
        with self.assertRaises(SessionError):
            self.load()
        self.assertEqual(other.read_text(), "private-secret")
        self.path.unlink()
        self.load()
        self.path.chmod(0o660)
        with self.assertRaises(SessionError):
            self.load()

    def test_failed_atomic_replace_preserves_old_file_and_removes_temporary(self):
        session = self.load()
        original = self.path.read_bytes()
        with patch("okx_bot.session.os.replace", side_effect=OSError("private-secret")):
            with self.assertRaises(SessionError) as raised:
                session.observe(D("5500"), D(".05"))
        self.assertNotIn("private-secret", str(raised.exception))
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.directory.glob(".session-*")), [])

    def test_invalid_values_cannot_update_persisted_state(self):
        session = self.load()
        before = self.path.read_bytes()
        for equity, threshold in ((D("-1"), D(".05")), (D("NaN"), D(".05")),
                                  (D("Infinity"), D(".05")), (True, D(".05")),
                                  (D("5000"), D(0)), (D("5000"), D(".51"))):
            with self.subTest(equity=equity, threshold=threshold), self.assertRaises(SessionError):
                session.observe(equity, threshold)
            self.assertEqual(self.path.read_bytes(), before)

    def test_fingerprint_normalizes_decimal_spelling_and_rejects_other_objects(self):
        profile = PortfolioConfig()
        self.assertEqual(profile_fingerprint(profile),
                         profile_fingerprint(replace(profile, risk_per_trade=D("0.0050"))))
        self.assertNotEqual(profile_fingerprint(profile),
                            profile_fingerprint(replace(profile, max_notional=D("150"))))
        with self.assertRaises(SessionError):
            profile_fingerprint(object())


if __name__ == "__main__":
    unittest.main()
