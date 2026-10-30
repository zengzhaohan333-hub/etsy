"""Private file credentials for the Linux service; never secret environment values.

The loader accepts systemd's private, read-only credential copies. The setup
command prompts on the server's terminal and stages a complete credential bundle
before replacing its directory. It never prints, exports or puts secrets in argv.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import fcntl
import getpass
import os
from pathlib import Path
import shutil
import stat
import sys
import tempfile
import uuid
import warnings

from .okx import Credentials, OkxError


CREDENTIAL_NAMES = ("api_key", "api_secret", "api_passphrase")
MAX_CREDENTIAL_BYTES = 8192
_DIRECTORY_MODES = {0o500, 0o700}
_FILE_MODES = {0o400, 0o600}


class ServiceCredentialError(ValueError):
    """A credential-free explanation safe for the service log or console."""


def _trusted_owner(info: os.stat_result) -> bool:
    return info.st_uid in {0, os.geteuid()}


def _open_directory(directory: Path) -> int:
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        info = os.fstat(descriptor)
        if not _trusted_owner(info) or stat.S_IMODE(info.st_mode) not in _DIRECTORY_MODES:
            raise ServiceCredentialError("凭证目录须由当前用户或 root 所有，权限须为 0700 或只读 0500。")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _read_credential(directory_fd: int, name: str) -> str:
    # NONBLOCK avoids hanging if a damaged store contains a FIFO. O_NOFOLLOW
    # and a pinned parent descriptor prevent path replacement/symlink escapes.
    descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK,
                         dir_fd=directory_fd)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or not _trusted_owner(info)
                or stat.S_IMODE(info.st_mode) not in _FILE_MODES
                or not 0 < info.st_size <= MAX_CREDENTIAL_BYTES):
            raise ServiceCredentialError("凭证须为当前用户或 root 所有的普通文件，权限为 0600/0400，大小不超过 8 KiB。")
        with os.fdopen(descriptor, "rb", closefd=False) as stream:
            content = stream.read(MAX_CREDENTIAL_BYTES + 1)
        if len(content) > MAX_CREDENTIAL_BYTES:
            raise ServiceCredentialError("凭证文件大小不符合要求。")
        # A single conventional file terminator is accepted. Other control
        # characters are rejected by Credentials, never silently normalized.
        if content.endswith(b"\r\n"):
            content = content[:-2]
        elif content.endswith(b"\n"):
            content = content[:-1]
        return content.decode("utf-8")
    finally:
        os.close(descriptor)


def load_service_credentials(directory: Path | None = None) -> Credentials:
    """Read exactly three fixed files; no OKX_API_* environment fallback.

    ``CREDENTIALS_DIRECTORY`` contains only systemd's non-secret directory path.
    The three values remain in the returned Credentials object, in memory.
    """
    descriptor = None
    try:
        if directory is None:
            selected = os.environ.get("CREDENTIALS_DIRECTORY")
            if not selected:
                raise ServiceCredentialError("未配置服务凭证目录；请使用 systemd LoadCredential 或明确指定目录。")
            directory = Path(selected)
        descriptor = _open_directory(Path(directory))
        values = [_read_credential(descriptor, name) for name in CREDENTIAL_NAMES]
        return Credentials(*values)
    except ServiceCredentialError:
        raise
    except (OSError, UnicodeError, TypeError, OkxError, ValueError):
        raise ServiceCredentialError("无法读取有效的服务凭证；请检查三个固定文件、所有者及私有权限。") from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _outside_checkout(directory: Path) -> bool:
    """Do not provision plaintext credentials anywhere under a Git checkout."""
    directory = directory.resolve(strict=False)
    for parent in (directory, *directory.parents):
        metadata = parent / ".git"
        if metadata.is_file() or (metadata.is_dir() and (metadata / "HEAD").exists()):
            return False
    return True


def _inspect_destination(directory: Path, replace: bool) -> bool:
    if not _outside_checkout(directory):
        raise ServiceCredentialError("密钥目录不能位于 Git 仓库内；请使用 /etc/okxbot/credentials 等专用目录。")
    if not os.path.lexists(directory):
        return False
    descriptor = _open_directory(directory)
    try:
        names = set(os.listdir(descriptor))
        if names - set(CREDENTIAL_NAMES):
            raise ServiceCredentialError("目标目录包含其他文件；不会覆盖或删除，请选择专用凭证目录。")
        if names and not replace:
            raise ServiceCredentialError("凭证已存在；如需更换，请先停止服务，再明确使用 --replace。")
        for name in names:
            info = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or not _trusted_owner(info)
                    or stat.S_IMODE(info.st_mode) not in _FILE_MODES):
                raise ServiceCredentialError("已有凭证类型或权限不安全；不会替换，请先检查专用目录。")
        return True
    finally:
        os.close(descriptor)


@contextmanager
def _setup_lock(parent: Path):
    info = parent.stat()
    if (not stat.S_ISDIR(info.st_mode) or not _trusted_owner(info)
            or stat.S_IMODE(info.st_mode) & 0o022):
        raise ServiceCredentialError("凭证父目录须由当前用户或 root 所有，且不能允许其他用户写入。")
    for ancestor in parent.parents:
        info = ancestor.stat()
        # A sticky system temporary directory is allowed above an owned private
        # parent. Its UID may be mapped by a container/user namespace.
        if stat.S_IMODE(info.st_mode) & 0o022 and not info.st_mode & stat.S_ISVTX:
            raise ServiceCredentialError("凭证目录的上级路径可被其他用户替换，请选择安全的专用目录。")
    descriptor = os.open(parent / ".okx-credentials-setup.lock",
                         os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, 0o600)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or not _trusted_owner(info)
                or stat.S_IMODE(info.st_mode) != 0o600):
            raise ServiceCredentialError("凭证配置锁的类型或权限不安全。")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ServiceCredentialError("另一个凭证配置进程正在运行，请稍后再试。") from None
        yield
    finally:
        os.close(descriptor)


def _sync_directory(directory: Path):
    descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _discard_previous_bundle(directory: Path):
    descriptor = _open_directory(directory)
    try:
        names = set(os.listdir(descriptor))
        if names - set(CREDENTIAL_NAMES):
            raise ServiceCredentialError("新凭证已完整保存；旧私有副本出现其他文件，已保留，请人工检查。")
        for name in names:
            os.unlink(name, dir_fd=descriptor)
    finally:
        os.close(descriptor)
    os.rmdir(directory)


def write_service_credentials(directory: Path, credentials: Credentials, *, replace: bool = False) -> None:
    """Stage all fields, then switch the bundle; failures restore the old store.

    Provision while the service is stopped. A concurrent reader either pins a
    complete directory or fails closed during the brief directory switch.
    Unknown files are never removed. Failed staging leaves the old store intact.
    """
    stage: Path | None = None
    previous: Path | None = None
    try:
        # Validate the complete bundle before even creating a staging directory.
        credentials = Credentials(credentials.api_key, credentials.api_secret, credentials.passphrase)
        values = [credentials.api_key.encode("utf-8"), credentials.api_secret.encode("utf-8"),
                  credentials.passphrase.encode("utf-8")]
        if any(not value or len(value) > MAX_CREDENTIAL_BYTES for value in values):
            raise ServiceCredentialError("每个凭证字段必须非空且不超过 8 KiB。")
        requested = Path(os.path.abspath(directory))
        directory = requested.parent.resolve(strict=False) / requested.name
        _inspect_destination(directory, replace)
        directory.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with _setup_lock(directory.parent):
            exists = _inspect_destination(directory, replace)
            stage = Path(tempfile.mkdtemp(prefix=".okx-credentials-stage-", dir=directory.parent))
            os.chmod(stage, 0o700)
            for name, value in zip(CREDENTIAL_NAMES, values):
                descriptor = os.open(stage / name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
                with os.fdopen(descriptor, "wb") as stream:
                    os.fchmod(stream.fileno(), 0o600)
                    stream.write(value)
                    stream.flush()
                    os.fsync(stream.fileno())
            _sync_directory(stage)
            if exists:
                previous = directory.parent / (".okx-credentials-previous-" + uuid.uuid4().hex)
                os.rename(directory, previous)
            try:
                os.rename(stage, directory)
            except BaseException:
                if previous is not None:
                    os.rename(previous, directory)
                    previous = None
                raise
            stage = None
            _sync_directory(directory.parent)
            if previous is not None:
                _discard_previous_bundle(previous)
                previous = None
    except ServiceCredentialError:
        raise
    except (OSError, ValueError, TypeError, RuntimeError, OkxError):
        raise ServiceCredentialError("凭证保存未完成；已保留原目录或私有恢复副本，请检查权限后再试。") from None
    finally:
        if previous is not None and not os.path.lexists(directory):
            # Also restore if a terminal interrupt arrives between the two
            # directory renames. A failed rollback leaves the private backup.
            try:
                os.rename(previous, directory)
            except OSError:
                pass
        if stage is not None:
            # This path is solely the private staging directory created above.
            shutil.rmtree(stage, ignore_errors=True)


def _collect_credentials() -> Credentials:
    if not sys.stdin.isatty():
        raise ServiceCredentialError("请直接在服务器交互终端运行，不能通过管道输入密钥。")
    prompts = ("欧易模拟盘 API Key（隐藏输入）：", "欧易模拟盘 Secret Key（隐藏输入）：",
               "欧易模拟盘 Passphrase（隐藏输入）：")
    values = []
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        for prompt in prompts:
            try:
                values.append(getpass.getpass(prompt, stream=sys.stderr))
            except getpass.GetPassWarning:
                raise ServiceCredentialError("此终端无法关闭输入回显，已取消；请使用正常 SSH 交互终端。") from None
    try:
        return Credentials(*values)
    except OkxError:
        raise ServiceCredentialError("凭证格式不正确；请复制模拟盘 API 的原始字段，不要包含换行或说明文字。") from None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="在服务器安全配置 OKX 模拟盘服务凭证，不启动交易")
    parser.add_argument("--directory", type=Path, required=True, help="仓库外的专用私有目录")
    parser.add_argument("--replace", action="store_true", help="明确更换已有凭证；先停止服务")
    args = parser.parse_args(argv)
    try:
        # Fail before prompting if an existing/user directory cannot be used.
        directory = Path(os.path.abspath(args.directory))
        _inspect_destination(directory, args.replace)
        if args.replace:
            print("更换凭证前请先停止服务。此操作不会启动或停止机器人，也不会改动订单日志。")
        print("仅在服务器本机保存三项模拟盘凭证，目录 0700、文件 0600；输入不会显示。")
        credentials = _collect_credentials()
        write_service_credentials(directory, credentials, replace=args.replace)
        print("模拟盘服务凭证已保存。没有启动交易；请继续进行只读检查。")
        return 0
    except ServiceCredentialError as error:
        print(f"无法继续：{error}", file=sys.stderr)
        return 78
    except (EOFError, KeyboardInterrupt):
        print("已取消，没有启动交易。", file=sys.stderr)
        return 130
    except Exception:
        # Never render arbitrary exceptions originating from secret input/I/O.
        print("凭证配置未完成；错误详情未输出，以免泄露密钥。", file=sys.stderr)
        return 78


if __name__ == "__main__":
    raise SystemExit(main())
