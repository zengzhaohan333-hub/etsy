from contextlib import redirect_stderr, redirect_stdout
import fcntl
import io
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import warnings

from okx_bot.okx import Credentials
from okx_bot.service_credentials import (
    CREDENTIAL_NAMES, MAX_CREDENTIAL_BYTES, ServiceCredentialError,
    load_service_credentials, main, write_service_credentials,
)


class ServiceCredentialTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.parent = Path(self.temp.name)
        self.path = self.parent / "credentials"
        self.old = Credentials("fixture-api-key", "fixture-api-secret", "fixture-passphrase")
        self.new = Credentials("replacement-key", "replacement-secret", "replacement-passphrase")

    def make_store(self):
        write_service_credentials(self.path, self.old)

    def assert_old_bundle(self):
        self.assertEqual(load_service_credentials(self.path), self.old)
        self.assertEqual(set(p.name for p in self.path.iterdir()), set(CREDENTIAL_NAMES))

    def test_private_bundle_round_trip_and_systemd_path(self):
        self.make_store()
        with patch.dict(os.environ, {"CREDENTIALS_DIRECTORY": str(self.path),
                                    "OKX_API_KEY": "must-not-be-read"}):
            loaded = load_service_credentials()
        self.assertEqual(loaded, self.old)
        self.assertNotIn(self.old.api_key, repr(loaded))
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o700)
        for name in CREDENTIAL_NAMES:
            self.assertEqual(stat.S_IMODE((self.path / name).stat().st_mode), 0o600)

    def test_no_secret_environment_fallback(self):
        with patch.dict(os.environ, {"OKX_API_KEY": "environment-key",
                                    "OKX_API_SECRET": "environment-secret",
                                    "OKX_API_PASSPHRASE": "environment-pass"}, clear=True):
            with self.assertRaises(ServiceCredentialError):
                load_service_credentials()

    def test_systemd_readonly_credentials_are_accepted(self):
        self.make_store()
        for name in CREDENTIAL_NAMES:
            (self.path / name).chmod(0o400)
        self.path.chmod(0o500)
        try:
            self.assertEqual(load_service_credentials(self.path), self.old)
        finally:
            self.path.chmod(0o700)

    def test_loader_rejects_public_directory_or_files(self):
        self.make_store()
        self.path.chmod(0o755)
        with self.assertRaises(ServiceCredentialError):
            load_service_credentials(self.path)
        self.path.chmod(0o700)
        (self.path / "api_key").chmod(0o644)
        with self.assertRaises(ServiceCredentialError):
            load_service_credentials(self.path)

    def test_loader_rejects_untrusted_owner(self):
        self.make_store()
        real_fstat = os.fstat

        def untrusted(descriptor):
            values = list(real_fstat(descriptor))
            values[4] = 987654
            return os.stat_result(values)

        with patch("okx_bot.service_credentials.os.fstat", side_effect=untrusted):
            with self.assertRaises(ServiceCredentialError):
                load_service_credentials(self.path)

    def test_loader_validates_each_file_owner_after_directory_check(self):
        self.make_store()
        real_fstat = os.fstat

        def untrusted_file(descriptor):
            info = real_fstat(descriptor)
            if stat.S_ISREG(info.st_mode):
                values = list(info)
                values[4] = 987654
                return os.stat_result(values)
            return info

        with patch("okx_bot.service_credentials.os.fstat", side_effect=untrusted_file):
            with self.assertRaises(ServiceCredentialError):
                load_service_credentials(self.path)

    def test_loader_does_not_follow_directory_symlink(self):
        self.make_store()
        link = self.parent / "directory-link"
        link.symlink_to(self.path, target_is_directory=True)
        with self.assertRaises(ServiceCredentialError):
            load_service_credentials(link)

    def test_loader_does_not_follow_secret_file_symlink(self):
        self.make_store()
        secret = self.path / "api_secret"
        saved = self.parent / "different-secret"
        secret.rename(saved)
        secret.symlink_to(saved)
        with self.assertRaises(ServiceCredentialError):
            load_service_credentials(self.path)

    def test_fifo_is_rejected_without_open_blocking(self):
        self.make_store()
        target = self.path / "api_key"
        target.unlink()
        os.mkfifo(target, 0o600)
        with self.assertRaises(ServiceCredentialError):
            load_service_credentials(self.path)

    def test_missing_oversized_empty_and_control_values_are_rejected(self):
        self.make_store()
        target = self.path / "api_key"
        for content in (b"", b"x" * (MAX_CREDENTIAL_BYTES + 1), b"bad\nheader", b"\xff"):
            target.write_bytes(content)
            with self.assertRaises(ServiceCredentialError):
                load_service_credentials(self.path)
        target.unlink()
        with self.assertRaises(ServiceCredentialError):
            load_service_credentials(self.path)

    def test_single_file_line_terminator_is_supported(self):
        self.make_store()
        for name in CREDENTIAL_NAMES:
            path = self.path / name
            path.write_bytes(path.read_bytes() + b"\r\n")
        self.assertEqual(load_service_credentials(self.path), self.old)

    def test_loader_reads_only_fixed_names(self):
        self.make_store()
        os.mkfifo(self.path / "must-not-open", 0o600)
        self.assertEqual(load_service_credentials(self.path), self.old)

    def test_permissions_private_even_under_permissive_umask(self):
        previous = os.umask(0)
        try:
            self.make_store()
        finally:
            os.umask(previous)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o700)
        for child in self.path.iterdir():
            self.assertEqual(stat.S_IMODE(child.stat().st_mode), 0o600)

    def test_existing_bundle_requires_explicit_replace(self):
        self.make_store()
        with self.assertRaises(ServiceCredentialError):
            write_service_credentials(self.path, self.new)
        self.assert_old_bundle()
        write_service_credentials(self.path, self.new, replace=True)
        self.assertEqual(load_service_credentials(self.path), self.new)
        self.assertFalse(list(self.parent.glob(".okx-credentials-previous-*")))

    def test_refuses_unknown_files_even_with_replace(self):
        self.make_store()
        extra = self.path / "user-notes"
        extra.write_text("preserve me")
        with self.assertRaises(ServiceCredentialError):
            write_service_credentials(self.path, self.new, replace=True)
        self.assertEqual(extra.read_text(), "preserve me")
        self.assertEqual(load_service_credentials(self.path), self.old)

    def test_refuses_git_checkout_before_creating_any_credentials(self):
        metadata = self.parent / ".git"
        metadata.mkdir()
        (metadata / "HEAD").write_text("ref: refs/heads/main\n")
        with self.assertRaises(ServiceCredentialError):
            self.make_store()
        self.assertFalse(self.path.exists())

    def test_refuses_checkout_reached_through_parent_symlink(self):
        repository = self.parent / "repository"
        (repository / ".git").mkdir(parents=True)
        (repository / ".git" / "HEAD").write_text("ref: refs/heads/main\n")
        alias = self.parent / "alias"
        alias.symlink_to(repository, target_is_directory=True)
        with self.assertRaises(ServiceCredentialError):
            write_service_credentials(alias / "credentials", self.old)
        self.assertFalse((repository / "credentials").exists())

    def test_refuses_world_writable_ancestor(self):
        untrusted = self.parent / "untrusted"
        untrusted.mkdir(mode=0o777)
        untrusted.chmod(0o777)
        private = untrusted / "private"
        private.mkdir(mode=0o700)
        with self.assertRaises(ServiceCredentialError):
            write_service_credentials(private / "credentials", self.old)
        self.assertFalse((private / "credentials").exists())

    def test_staging_failure_does_not_modify_old_bundle(self):
        self.make_store()
        with patch("okx_bot.service_credentials.os.fsync", side_effect=OSError("private failure")):
            with self.assertRaises(ServiceCredentialError):
                write_service_credentials(self.path, self.new, replace=True)
        self.assert_old_bundle()
        self.assertFalse(list(self.parent.glob(".okx-credentials-stage-*")))

    def test_directory_switch_failure_restores_complete_old_bundle(self):
        self.make_store()
        original_rename = os.rename
        failed = False

        def rename_once(source, destination):
            nonlocal failed
            if Path(source).name.startswith(".okx-credentials-stage-") and not failed:
                failed = True
                raise OSError("private rename error")
            return original_rename(source, destination)

        with patch("okx_bot.service_credentials.os.rename", side_effect=rename_once):
            with self.assertRaises(ServiceCredentialError):
                write_service_credentials(self.path, self.new, replace=True)
        self.assert_old_bundle()
        self.assertFalse(list(self.parent.glob(".okx-credentials-stage-*")))
        self.assertFalse(list(self.parent.glob(".okx-credentials-previous-*")))

    def test_terminal_interrupt_between_directory_renames_restores_store(self):
        self.make_store()
        original_rename = os.rename

        def interrupt_after_old_move(source, destination):
            result = original_rename(source, destination)
            if Path(source) == self.path:
                raise KeyboardInterrupt()
            return result

        with patch("okx_bot.service_credentials.os.rename", side_effect=interrupt_after_old_move):
            with self.assertRaises(KeyboardInterrupt):
                write_service_credentials(self.path, self.new, replace=True)
        self.assert_old_bundle()
        self.assertFalse(list(self.parent.glob(".okx-credentials-stage-*")))

    def test_unexpected_file_added_during_rotation_is_not_deleted(self):
        self.make_store()
        original_rename = os.rename

        def add_user_file(source, destination):
            result = original_rename(source, destination)
            if Path(source) == self.path:
                (Path(destination) / "user-note").write_text("preserve this concurrent file")
            return result

        with patch("okx_bot.service_credentials.os.rename", side_effect=add_user_file):
            with self.assertRaises(ServiceCredentialError):
                write_service_credentials(self.path, self.new, replace=True)
        self.assertEqual(load_service_credentials(self.path), self.new)
        previous = list(self.parent.glob(".okx-credentials-previous-*"))
        self.assertEqual(len(previous), 1)
        self.assertEqual((previous[0] / "user-note").read_text(), "preserve this concurrent file")

    def test_invalid_complete_bundle_never_creates_destination(self):
        invalid = SimpleNamespace(api_key="valid", api_secret="bad\nsecret", passphrase="valid")
        with self.assertRaises(ServiceCredentialError):
            write_service_credentials(self.path, invalid)
        self.assertFalse(self.path.exists())
        self.assertFalse(list(self.parent.glob(".okx-credentials-*")))

    def test_setup_lock_prevents_competing_writer(self):
        self.make_store()
        lock = self.parent / ".okx-credentials-setup.lock"
        with lock.open("r+b") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaises(ServiceCredentialError):
                write_service_credentials(self.path, self.new, replace=True)
        self.assert_old_bundle()

    def run_main(self, arguments, inputs=(), tty=True, failure=None):
        output = io.StringIO()
        errors = io.StringIO()
        with (redirect_stdout(output), redirect_stderr(errors),
              patch("okx_bot.service_credentials.sys.stdin.isatty", return_value=tty),
              patch("okx_bot.service_credentials.getpass.getpass", side_effect=failure or list(inputs)) as prompt):
            result = main(arguments)
        return result, output.getvalue() + errors.getvalue(), prompt

    def test_cli_hidden_inputs_are_not_logged_or_exported(self):
        fields = (self.old.api_key, self.old.api_secret, self.old.passphrase)
        with patch.dict(os.environ, {}, clear=True):
            result, logs, prompt = self.run_main(["--directory", str(self.path)], fields)
            self.assertNotIn("OKX_API_KEY", os.environ)
        self.assertEqual(result, 0)
        self.assertEqual(prompt.call_count, 3)
        for value in fields:
            self.assertNotIn(value, logs)
        self.assert_old_bundle()

    def test_cli_existing_store_fails_before_prompt(self):
        self.make_store()
        result, _, prompt = self.run_main(["--directory", str(self.path)])
        self.assertEqual(result, 78)
        prompt.assert_not_called()
        self.assert_old_bundle()

    def test_cli_rejects_pipe_without_prompt_or_files(self):
        result, _, prompt = self.run_main(["--directory", str(self.path)], tty=False)
        self.assertEqual(result, 78)
        prompt.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_cli_refuses_visible_getpass_fallback(self):
        def visible_fallback(*_args, **_kwargs):
            warnings.warn("Echo fallback", __import__("getpass").GetPassWarning)
            return "must-not-be-read"

        result, _, prompt = self.run_main(["--directory", str(self.path)], failure=visible_fallback)
        self.assertEqual(result, 78)
        self.assertEqual(prompt.call_count, 1)
        self.assertFalse(self.path.exists())

    def test_cli_redacts_arbitrary_input_exception(self):
        secret = "secret-that-must-not-be-logged"
        result, logs, _ = self.run_main(["--directory", str(self.path)], failure=OSError(secret))
        self.assertEqual(result, 78)
        self.assertNotIn(secret, logs)
        self.assertFalse(self.path.exists())


if __name__ == "__main__":
    unittest.main()
