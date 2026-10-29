import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from okx_bot.__main__ import main


class CliTests(unittest.TestCase):
    def test_demo_cannot_submit_without_ack(self):
        with contextlib.redirect_stderr(io.StringIO()) as err, patch('okx_bot.__main__.Credentials.from_env') as creds:
            self.assertEqual(main(['demo']), 1)
            self.assertIn('--ack-demo-orders', err.getvalue())
            creds.assert_not_called()

    def test_demo_check_missing_credentials_exits_cleanly(self):
        with patch.dict('os.environ', {}, clear=True), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(['demo-check']), 1)
            self.assertIn('Missing OKX demo API credentials', err.getvalue())
            self.assertNotIn('Traceback', err.getvalue())

    def test_journal_recovery_requires_explicit_flag(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(['demo-reconcile']), 1)
            self.assertIn('--ack-journal', err.getvalue())

    def test_end_to_end_paper_command(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(main(['paper', '--bars', '80', '--state-dir', tmp]), 0)
            result = json.loads(output.getvalue())
            self.assertEqual(result['processed_bars'], 80)
            self.assertGreater(result['total_trades'], 0)
            status = json.loads((Path(tmp) / 'status.json').read_text())
            self.assertIsNone(status['position'])

    def test_live_command_not_supported(self):
        with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
            main(['live'])
        self.assertEqual(error.exception.code, 2)

    def test_invalid_config_is_actionable(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(main(['paper', '--config', '/nonexistent/config.toml']), 1)
            self.assertNotIn('Traceback', err.getvalue())


if __name__ == '__main__':
    unittest.main()
