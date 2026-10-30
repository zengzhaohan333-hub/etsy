#!/usr/bin/env bash
# Install local source without network/package-manager calls or starting a bot.
set -euo pipefail
umask 077

usage() {
    cat <<'HELP'
Usage: sudo bash deploy/linux/install.sh [--source DIRECTORY] [--replace-units]

Prepare a native Linux systemd deployment. Requires Python 3.11+, systemd 247+
and an already downloaded project. Ubuntu 24.04 is recommended.
WSL is rejected; run this only in the target independent server's SSH terminal.
No packages are downloaded, no credentials are collected, no services are started.
Existing configuration and state are retained. Stop both installed services
before upgrading. Differing service units require --replace-units (with backup).
HELP
}

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
OKX_SOURCE_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)"
OKX_REPLACE_UNITS=0
while (($#)); do
    case "$1" in
        --help|-h) usage; exit 0 ;;
        --source)
            (($# >= 2)) || fail '--source needs a directory.'
            OKX_SOURCE_ROOT="$2"
            shift 2
            ;;
        --replace-units) OKX_REPLACE_UNITS=1; shift ;;
        *) fail "Unknown argument: $1" ;;
    esac
done

# Detect WSL before privilege checks, package probes, directory creation or any
# deployment mutation. Starting this installer on the user's PC is never setup.
OKX_KERNEL_NAME="$(uname -s)"
OKX_KERNEL_RELEASE="$(uname -r)"
OKX_PROC_RELEASE=''
if [[ -r /proc/sys/kernel/osrelease ]]; then
    IFS= read -r OKX_PROC_RELEASE < /proc/sys/kernel/osrelease
fi
case "${OKX_KERNEL_RELEASE,,} ${OKX_PROC_RELEASE,,}" in
    *microsoft*|*wsl*) fail '此部署入口用于独立Linux服务器，当前为WSL，请在目标SSH终端执行' ;;
esac
[[ "$OKX_KERNEL_NAME" == Linux ]] || fail 'This installer requires an independent native Linux server.'
[[ ${EUID} -eq 0 ]] || fail 'Run this installer explicitly with sudo.'
[[ -d /run/systemd/system ]] || fail 'A running native Linux systemd installation is required.'
for OKX_COMMAND in python3 systemctl install cmp mktemp flock getent useradd groupadd runuser; do
    command -v "$OKX_COMMAND" >/dev/null || fail "Required command is missing: $OKX_COMMAND"
done
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' ||
    fail 'Python 3.11+ is required; use Ubuntu 24.04 or install a supported Python first.'
python3 -c 'import venv' || fail 'Install the distribution python3-venv package, then retry.'
read -r OKX_SYSTEMD_LABEL OKX_SYSTEMD_VERSION OKX_SYSTEMD_REST < <(systemctl --version)
[[ "$OKX_SYSTEMD_VERSION" =~ ^[0-9]+$ && "$OKX_SYSTEMD_VERSION" -ge 247 ]] ||
    fail 'systemd 247+ with LoadCredential support is required.'

OKX_SOURCE_ROOT="$(cd -- "$OKX_SOURCE_ROOT" && pwd -P)"
for OKX_SOURCE_FILE in okx_bot/__init__.py okx_bot/service.py okx_bot/service_credentials.py \
    config.portfolio.toml pyproject.toml deploy/linux/okxbot.service deploy/linux/okxbot-monitor.service; do
    [[ -f "$OKX_SOURCE_ROOT/$OKX_SOURCE_FILE" && ! -L "$OKX_SOURCE_ROOT/$OKX_SOURCE_FILE" ]] ||
        fail "Missing regular project file: $OKX_SOURCE_FILE. Extract the full updated project."
done

[[ ! -L /run/okxbot-installer ]] || fail 'Refusing a symlinked installer lock directory.'
install -d -o root -g root -m 0700 /run/okxbot-installer
exec 9>/run/okxbot-installer/lock
flock -n 9 || fail 'Another OKX bot installer is running.'
for OKX_UNIT in okxbot.service okxbot-monitor.service; do
    OKX_ACTIVE_STATE="$(systemctl show "$OKX_UNIT" --property=ActiveState --value)" ||
        fail "Cannot inspect $OKX_UNIT; no deployment changes were made."
    case "$OKX_ACTIVE_STATE" in
        active|activating|deactivating|reloading)
            fail "Stop $OKX_UNIT and inspect exchange positions before installing or upgrading."
            ;;
    esac
    OKX_UNIT_TARGET="/etc/systemd/system/$OKX_UNIT"
    [[ ! -L "$OKX_UNIT_TARGET" ]] || fail "Refusing to replace a symlinked unit: $OKX_UNIT"
    if [[ -e "$OKX_UNIT_TARGET" ]] && ! cmp -s -- "$OKX_SOURCE_ROOT/deploy/linux/$OKX_UNIT" "$OKX_UNIT_TARGET"; then
        [[ "$OKX_REPLACE_UNITS" -eq 1 ]] ||
            fail "$OKX_UNIT differs from this project. Review it, then use --replace-units to back it up and replace it."
    fi
done

# Do not repurpose an existing interactive account or follow managed symlinks.
for OKX_DIRECTORY in /opt/okxbot /opt/okxbot/app /opt/okxbot/venv /etc/okxbot /etc/okxbot/credentials /var/lib/okxbot; do
    [[ ! -L "$OKX_DIRECTORY" ]] || fail "Refusing a symlinked installation directory: $OKX_DIRECTORY"
done
python3 - <<'PY'
from pathlib import Path
import os, stat
for name in ('/opt/okxbot', '/etc/okxbot'):
    path = Path(name)
    if path.exists():
        value = path.stat()
        if not stat.S_ISDIR(value.st_mode) or value.st_uid != 0 or value.st_mode & 0o022:
            raise SystemExit('Existing installation parent must be a root-owned directory without group/world write access.')
for name in ('/opt/okxbot/app', '/opt/okxbot/venv'):
    path = Path(name)
    if not path.exists():
        continue
    for directory, subdirs, files in os.walk(path, followlinks=False):
        for item in [Path(directory), *(Path(directory) / child for child in subdirs + files)]:
            value = item.lstat()
            if value.st_uid != 0:
                raise SystemExit('Existing application/venv contains files not owned by root; inspect before running privileged helpers.')
            if item.is_symlink():
                value = item.stat()
            if value.st_uid != 0 or value.st_mode & 0o022:
                raise SystemExit('Existing application/venv is writable by an unprivileged account; inspect before running privileged helpers.')
            if name.endswith('/venv') and stat.S_ISDIR(value.st_mode) and value.st_mode & 0o055 != 0o055:
                raise SystemExit('Existing venv directories are not readable/traversable by okxbot. Stop services, back up and recreate the venv; this installer will not chmod existing files.')
venv_config = Path('/opt/okxbot/venv/pyvenv.cfg')
if venv_config.exists() and venv_config.stat().st_mode & 0o044 != 0o044:
    raise SystemExit('Existing venv/pyvenv.cfg is not readable by okxbot. Back up and recreate the stopped venv; permissions were not widened.')
config = Path('/etc/okxbot/config.portfolio.toml')
if config.is_symlink():
    raise SystemExit('Refusing a symlinked strategy configuration.')
if config.exists():
    value = config.stat()
    if not stat.S_ISREG(value.st_mode) or value.st_uid != 0 or value.st_mode & 0o022:
        raise SystemExit('Existing strategy configuration must be a root-owned regular file without group/world write access.')
credentials = Path('/etc/okxbot/credentials')
if credentials.exists():
    value = credentials.stat()
    if not stat.S_ISDIR(value.st_mode) or value.st_uid != 0 or value.st_mode & 0o077:
        raise SystemExit('Existing credentials directory must belong to root and have mode 0700; no credentials were read.')
PY
if getent passwd okxbot >/dev/null; then
    IFS=: read -r OKX_USER_NAME OKX_USER_PASSWORD OKX_USER_ID OKX_USER_GROUP_ID OKX_USER_DESCRIPTION OKX_USER_HOME OKX_USER_SHELL < <(getent passwd okxbot)
    [[ "$OKX_USER_ID" != 0 && "$OKX_USER_HOME" == /var/lib/okxbot && "$OKX_USER_SHELL" == */nologin ]] ||
        fail 'Existing okxbot account is not this dedicated service account; no account was changed.'
    [[ "$(id -gn okxbot)" == okxbot ]] || fail 'Existing okxbot account needs primary group okxbot.'
else
    getent group okxbot >/dev/null || groupadd --system okxbot
    useradd --system --gid okxbot --home-dir /var/lib/okxbot --no-create-home --shell /usr/sbin/nologin okxbot
fi

install -d -o root -g root -m 0755 /opt/okxbot /etc/okxbot
if [[ ! -e /etc/okxbot/credentials ]]; then
    install -d -o root -g root -m 0700 /etc/okxbot/credentials
fi
if [[ ! -e /var/lib/okxbot ]]; then
    install -d -o okxbot -g okxbot -m 0700 /var/lib/okxbot
else
    python3 - <<'PY'
import os, pwd, stat
value = os.stat('/var/lib/okxbot', follow_symlinks=False)
if not stat.S_ISDIR(value.st_mode) or value.st_uid != pwd.getpwnam('okxbot').pw_uid or value.st_mode & 0o077:
    raise SystemExit('Existing /var/lib/okxbot must belong to okxbot and have mode 0700. Inspect it manually; no state files were changed.')
PY
fi
if [[ ! -e /etc/okxbot/config.portfolio.toml ]]; then
    install -o root -g root -m 0644 "$OKX_SOURCE_ROOT/config.portfolio.toml" /etc/okxbot/config.portfolio.toml
else
    printf '%s\n' 'Retaining existing /etc/okxbot/config.portfolio.toml unchanged.'
fi

if [[ ! -e /opt/okxbot/venv ]]; then
    (umask 022; python3 -m venv --without-pip /opt/okxbot/venv)
fi
/opt/okxbot/venv/bin/python -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' ||
    fail 'Existing venv is not Python 3.11+; inspect and replace it while the service is stopped.'

# A temporary directory belongs solely to this installer. Old app directories
# are retained as backups, including any unexpected local files inside them.
OKX_APP_STAGE="$(mktemp -d /opt/okxbot/.app-stage.XXXXXX)"
OKX_APP_BACKUP=''
cleanup() {
    if [[ -n "$OKX_APP_STAGE" && -d "$OKX_APP_STAGE" ]]; then
        rm -rf -- "$OKX_APP_STAGE"
    fi
    if [[ -n "$OKX_APP_BACKUP" && -d "$OKX_APP_BACKUP" && ! -e /opt/okxbot/app ]]; then
        mv -- "$OKX_APP_BACKUP" /opt/okxbot/app
    fi
}
trap cleanup EXIT
python3 - "$OKX_SOURCE_ROOT" "$OKX_APP_STAGE" <<'PY'
from pathlib import Path
import shutil, sys
source, target = map(Path, sys.argv[1:])
package = target / 'okx_bot'
package.mkdir(mode=0o755)
package.chmod(0o755)
for item in (source / 'okx_bot').iterdir():
    if item.suffix not in {'.py', '.html'}:
        continue
    if item.is_symlink() or not item.is_file():
        raise SystemExit('Refusing non-regular application source.')
    destination = package / item.name
    shutil.copyfile(item, destination)
    destination.chmod(0o644)
for name in ('pyproject.toml', 'config.portfolio.toml', 'config.paper.toml'):
    item = source / name
    if not item.is_file() or item.is_symlink():
        raise SystemExit('Missing regular application metadata/configuration.')
    shutil.copyfile(item, target / name)
    (target / name).chmod(0o644)
target.chmod(0o755)
PY

# Only a non-executable source path is installed in the otherwise clean venv.
# This lets python -m helper commands work from outside the application folder.
/opt/okxbot/venv/bin/python - <<'PY'
from pathlib import Path
import sysconfig
path = Path(sysconfig.get_path('purelib')) / 'okxbot-source.pth'
content = '/opt/okxbot/app\n'
if path.exists() and (path.is_symlink() or path.read_text() != content):
    raise SystemExit('Existing okxbot-source.pth differs. Inspect it manually before upgrading.')
path.write_text(content, encoding='utf-8')
path.chmod(0o644)
PY
if [[ -e /opt/okxbot/app ]]; then
    OKX_APP_BACKUP="$(mktemp -d /opt/okxbot/app.backup.XXXXXX)"
    rmdir -- "$OKX_APP_BACKUP"
    mv -- /opt/okxbot/app "$OKX_APP_BACKUP"
fi
mv -- "$OKX_APP_STAGE" /opt/okxbot/app
OKX_APP_STAGE=''
(
    cd -- /opt/okxbot/app
    runuser -u okxbot -- /opt/okxbot/venv/bin/python -I -B -c 'import okx_bot.service, okx_bot.service_credentials'
) || fail 'The service user cannot import the deployed application. Inspect venv/app permissions and the retained app backup; no services were started.'
if [[ -n "$OKX_APP_BACKUP" ]]; then
    printf 'Previous application retained at: %s\n' "$OKX_APP_BACKUP"
fi
for OKX_UNIT in okxbot.service okxbot-monitor.service; do
    OKX_UNIT_TARGET="/etc/systemd/system/$OKX_UNIT"
    if [[ -e "$OKX_UNIT_TARGET" ]] && ! cmp -s -- "$OKX_SOURCE_ROOT/deploy/linux/$OKX_UNIT" "$OKX_UNIT_TARGET"; then
        cp -p -- "$OKX_UNIT_TARGET" "$OKX_UNIT_TARGET.backup.$(date -u +%Y%m%dT%H%M%S%N)"
    fi
    install -o root -g root -m 0644 "$OKX_SOURCE_ROOT/deploy/linux/$OKX_UNIT" "$OKX_UNIT_TARGET"
done
systemctl daemon-reload
printf '%s\n' \
    'Installation prepared. No credentials were collected and no service was started or enabled.' \
    'Read deploy/linux/README.md, migrate the original journal safely, and provision credentials locally.' \
    'Credentials helper: sudo /opt/okxbot/venv/bin/python -I -m okx_bot.service_credentials --directory /etc/okxbot/credentials'
