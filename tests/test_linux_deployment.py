"""Offline checks for the privileged deployment contract; never install/start."""
from pathlib import Path
import configparser
import os
import re
import shlex
import shutil
import subprocess
import stat
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy" / "linux"


def unit(name):
    text = (DEPLOY / name).read_text(encoding="utf-8")
    parsed = configparser.ConfigParser(interpolation=None, strict=False)
    parsed.read_string(text)
    return text, parsed


class LinuxDeploymentTests(unittest.TestCase):
    def test_service_uses_fixed_demo_entry_and_protected_state(self):
        text, config = unit("okxbot.service")
        service = config["Service"]
        self.assertEqual(service["User"], "okxbot")
        self.assertEqual(service["Group"], "okxbot")
        self.assertEqual(service["WorkingDirectory"], "/opt/okxbot/app")
        self.assertEqual(shlex.split(service["ExecStart"]), [
            "/opt/okxbot/venv/bin/python", "-m", "okx_bot.service",
            "--config", "/etc/okxbot/config.portfolio.toml",
            "--state-dir", "/var/lib/okxbot", "--ack-demo-orders"])
        self.assertEqual(service["StateDirectory"], "okxbot")
        self.assertEqual(service["StateDirectoryMode"], "0700")
        self.assertEqual(service["UMask"], "0077")
        credentials = [line.split("=", 1)[1] for line in text.splitlines() if line.startswith("LoadCredential=")]
        self.assertEqual(set(credentials), {
            "api_key:/etc/okxbot/credentials/api_key",
            "api_secret:/etc/okxbot/credentials/api_secret",
            "api_passphrase:/etc/okxbot/credentials/api_passphrase"})
        self.assertEqual(len(credentials), 3)
        self.assertNotIn("EnvironmentFile", text)
        self.assertEqual(service["Environment"], "PYTHONUNBUFFERED=1")

    def test_failure_and_namespace_policy_cannot_restart_safety_halts(self):
        _, config = unit("okxbot.service")
        service = config["Service"]
        self.assertEqual(service["Restart"], "on-failure")
        prevented = set(service["RestartPreventExitStatus"].split())
        self.assertTrue({"1", "70", "78"}.issubset(prevented))
        self.assertNotIn("75", prevented)
        self.assertEqual(config["Unit"]["StartLimitBurst"], "3")
        self.assertEqual(config["Unit"]["StartLimitIntervalSec"], "600")
        self.assertEqual(service["PrivateTmp"], "false")
        self.assertIn("/tmp", service["ReadWritePaths"].split())
        self.assertIn("/var/lib/okxbot", service["ReadWritePaths"].split())
        self.assertEqual(service["ProtectSystem"], "strict")
        self.assertEqual(service["NoNewPrivileges"], "true")

    def test_monitor_is_separate_read_only_without_credentials(self):
        text, config = unit("okxbot-monitor.service")
        command = shlex.split(config["Service"]["ExecStart"])
        self.assertEqual(command[:3], ["/opt/okxbot/venv/bin/python", "-m", "okx_bot.monitor"])
        self.assertEqual(command[command.index("--status-file") + 1], "/var/lib/okxbot/status.json")
        self.assertEqual(command[command.index("--config-file") + 1], "/etc/okxbot/config.portfolio.toml")
        self.assertEqual(command[command.index("--port") + 1], "8766")
        self.assertNotIn("LoadCredential=", text)
        self.assertNotIn("ReadWritePaths=", text)
        self.assertNotIn("0.0.0.0", text)

    def test_installer_syntax_help_and_invalid_arguments_have_no_side_effects(self):
        script = DEPLOY / "install.sh"
        syntax = subprocess.run(["bash", "-n", str(script)], capture_output=True, text=True)
        self.assertEqual(syntax.returncode, 0, syntax.stderr)
        help_result = subprocess.run(["bash", str(script), "--help"], capture_output=True, text=True)
        self.assertEqual(help_result.returncode, 0, help_result.stderr)
        self.assertIn("No packages are downloaded", help_result.stdout)
        invalid = subprocess.run(["bash", str(script), "--start-now"], capture_output=True, text=True)
        self.assertNotEqual(invalid.returncode, 0)
        self.assertIn("Unknown argument", invalid.stderr)
        missing = subprocess.run(["bash", str(script), "--source"], capture_output=True, text=True)
        self.assertNotEqual(missing.returncode, 0)
        self.assertIn("needs a directory", missing.stderr)

    def test_installer_rejects_wsl_before_privilege_and_mutating_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            fake_bin = Path(directory)
            fake_uname = fake_bin / "uname"
            fake_uname.write_text('#!/bin/sh\nif [ "$1" = "-s" ]; then echo Linux; else echo 5.15.167.4-Microsoft-standard-WSL2; fi\n')
            fake_uname.chmod(0o755)
            marker = fake_bin / "mutation-was-attempted"
            fake_install = fake_bin / "install"
            fake_install.write_text('#!/bin/sh\nprintf blocked > "$OKX_INSTALL_TEST_MARKER"\nexit 99\n')
            fake_install.chmod(0o755)
            environment = dict(os.environ, PATH=str(fake_bin) + os.pathsep + os.environ.get("PATH", ""),
                               OKX_INSTALL_TEST_MARKER=str(marker))
            result = subprocess.run(["bash", str(DEPLOY / "install.sh")],
                                    env=environment, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("此部署入口用于独立Linux服务器，当前为WSL，请在目标SSH终端执行", result.stderr)
            self.assertFalse(marker.exists())

    def test_isolated_credential_helper_bootstraps_without_pip_or_application_cwd(self):
        with tempfile.TemporaryDirectory() as directory:
            temporary = Path(directory)
            venv_path = temporary / "venv"
            installer = (DEPLOY / "install.sh").read_text(encoding="utf-8")
            creation_line = next(line.strip() for line in installer.splitlines()
                                 if "-m venv --without-pip /opt/okxbot/venv" in line)
            creation_line = creation_line.replace("/opt/okxbot/venv", '"$1"')
            created = subprocess.run(["bash", "-c", "umask 077\n" + creation_line,
                                      "installer-venv-test", str(venv_path)],
                                     capture_output=True, text=True)
            self.assertEqual(created.returncode, 0, created.stderr)
            executable = venv_path / "bin" / "python"
            purelib = subprocess.check_output([
                str(executable), "-I", "-c",
                "import sysconfig; print(sysconfig.get_path('purelib'))"], text=True).strip()
            app = temporary / "app"
            app.mkdir(mode=0o700)
            copy_source = re.search(
                r'python3 - "\$OKX_SOURCE_ROOT" "\$OKX_APP_STAGE" <<\'PY\'\n(.*?)\nPY\n',
                installer, re.S).group(1)
            copied = subprocess.run([sys.executable, "-", str(ROOT), str(app)],
                                    input="import os\nos.umask(0o077)\n" + copy_source,
                                    capture_output=True, text=True)
            self.assertEqual(copied.returncode, 0, copied.stderr)
            pth_source = re.search(
                r'/opt/okxbot/venv/bin/python - <<\'PY\'\n(.*?)\nPY\n', installer, re.S).group(1)
            pth_source = pth_source.replace("content = '/opt/okxbot/app\\n'", "content = " + repr(str(app) + "\n"))
            registered = subprocess.run([str(executable), "-I", "-"],
                                        input="import os\nos.umask(0o077)\n" + pth_source,
                                        capture_output=True, text=True)
            self.assertEqual(registered.returncode, 0, registered.stderr)
            self.assertEqual(stat.S_IMODE((Path(purelib) / "okxbot-source.pth").stat().st_mode), 0o644)
            directories = [app, app / "okx_bot", venv_path, venv_path / "bin", venv_path / "lib"]
            current = Path(purelib)
            while current != venv_path:
                directories.append(current)
                current = current.parent
            for path in directories:
                self.assertEqual(stat.S_IMODE(path.stat().st_mode) & 0o055, 0o055, str(path))
            self.assertEqual(stat.S_IMODE((venv_path / "pyvenv.cfg").stat().st_mode) & 0o044, 0o044)
            self.assertEqual(stat.S_IMODE((app / "okx_bot" / "service_credentials.py").stat().st_mode), 0o644)
            empty_directory = temporary / "empty"
            empty_directory.mkdir()
            no_pip = subprocess.check_output([
                str(executable), "-I", "-c",
                "import importlib.util; print(importlib.util.find_spec('pip') is None)"],
                cwd=empty_directory, text=True).strip()
            self.assertEqual(no_pip, "True")
            result = subprocess.run([
                str(executable), "-I", "-m", "okx_bot.service_credentials", "--help"],
                cwd=empty_directory, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("--directory", result.stdout)

    @unittest.skipUnless(shutil.which("systemd-analyze"), "systemd-analyze is not installed")
    def test_systemd_parser_accepts_unit_directives_without_starting_services(self):
        with tempfile.TemporaryDirectory() as directory:
            paths = []
            for name in ("okxbot.service", "okxbot-monitor.service"):
                source = (DEPLOY / name).read_text(encoding="utf-8")
                lines = ["ExecStart=/usr/bin/true" if line.startswith("ExecStart=") else line
                         for line in source.splitlines()]
                path = Path(directory) / name
                path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                paths.append(str(path))
            result = subprocess.run(["systemd-analyze", "verify", "--man=no", *paths],
                                    capture_output=True, text=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
