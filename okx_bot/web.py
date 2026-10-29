"""Loopback dashboard for the local, synthetic paper-trading workflow only."""

from __future__ import annotations

import json
from pathlib import Path
import secrets
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


class PaperProcess:
    """Own exactly one child and never signal processes discovered elsewhere."""

    def __init__(self, config: Path, state_dir: Path):
        self.config = config.resolve()
        self.state_dir = state_dir.resolve()
        self.process: subprocess.Popen | None = None
        self.error: str | None = None
        self._lock = threading.Lock()

    def _running(self) -> bool:
        return self.process is not None and self.process.poll() is None

    def start(self) -> bool:
        with self._lock:
            if self._running():
                return False
            self.state_dir.mkdir(parents=True, exist_ok=True)
            self.error = None
            command = [
                sys.executable, "-m", "okx_bot", "paper",
                "--config", str(self.config), "--state-dir", str(self.state_dir),
                "--bars", "320", "--interval", "0.1",
            ]
            try:
                with (self.state_dir / "web-worker.log").open("ab") as log:
                    self.process = subprocess.Popen(
                        command, cwd=Path(__file__).resolve().parent.parent,
                        stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                        start_new_session=True,
                    )
            except OSError:
                self.error = "无法启动本地模拟进程，请检查 Python 和配置文件。"
                raise
            return True

    def stop(self) -> bool:
        with self._lock:
            if not self._running():
                return False
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
            return True

    def status(self) -> dict:
        with self._lock:
            running = self._running()
            state = None
            read_error = None
            try:
                path = self.state_dir / "status.json"
                if path.exists():
                    # A local file is still bounded so a broken worker cannot
                    # force the dashboard to load an unbounded response.
                    if path.stat().st_size > 2_000_000:
                        raise ValueError("oversized status")
                    candidate = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(candidate, dict):
                        raise ValueError("invalid status")
                    state = candidate
            except (OSError, ValueError):
                read_error = "暂时无法读取模拟状态，请查看运行日志。"
            if self.process and not running and self.process.returncode not in (0, -15, None):
                self.error = "模拟进程异常退出，请查看 .runtime/web-worker.log。"
            return {"running": running, "state": state, "error": self.error or read_error}


def handler_for(manager: PaperProcess, token: str):
    class Handler(BaseHTTPRequestHandler):
        server_version = "OKXLocalDashboard/1.0"

        def log_message(self, *_args):
            # Do not echo request headers, query strings or input into logs.
            pass

        def _valid_host(self) -> bool:
            port = self.server.server_address[1]
            return self.headers.get("Host") in {f"127.0.0.1:{port}", f"localhost:{port}"}

        def _send(self, code: int, content: bytes, content_type: str):
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self' 'unsafe-inline'; "
                "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
                "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'",
            )
            self.end_headers()
            self.wfile.write(content)

        def _json(self, code: int, data: dict):
            self._send(code, json.dumps(data, ensure_ascii=False).encode("utf-8"),
                       "application/json; charset=utf-8")

        def do_GET(self):
            if not self._valid_host():
                return self._json(403, {"error": "Host 不受信任"})
            path = urlsplit(self.path).path
            if path == "/":
                html = Path(__file__).with_name("dashboard.html").read_text(encoding="utf-8")
                return self._send(200, html.replace("__CSRF_TOKEN__", token).encode("utf-8"),
                                  "text/html; charset=utf-8")
            if path == "/api/status":
                return self._json(200, manager.status())
            return self._json(404, {"error": "未找到"})

        def do_POST(self):
            if not self._valid_host():
                return self._json(403, {"error": "Host 不受信任"})
            origin = self.headers.get("Origin")
            expected_origin = "http://" + self.headers.get("Host", "")
            if origin is not None and origin != expected_origin:
                return self._json(403, {"error": "Origin 不受信任"})
            supplied = self.headers.get("X-CSRF-Token", "")
            if not secrets.compare_digest(supplied.encode("utf-8"), token.encode("utf-8")):
                return self._json(403, {"error": "无效操作令牌"})
            if self.headers.get_content_type() != "application/json":
                return self._json(415, {"error": "需要 JSON 请求"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 1024:
                    return self._json(413, {"error": "请求过大"})
                body = json.loads(self.rfile.read(length))
                if body != {}:
                    return self._json(400, {"error": "不接受额外参数"})
            except (ValueError, UnicodeDecodeError):
                return self._json(400, {"error": "无效 JSON"})
            path = urlsplit(self.path).path
            try:
                if path == "/api/start":
                    started = manager.start()
                    return self._json(200, {"started": started, **manager.status()})
                if path == "/api/stop":
                    stopped = manager.stop()
                    return self._json(200, {"stopped": stopped, **manager.status()})
            except (OSError, subprocess.TimeoutExpired):
                return self._json(500, {"error": "操作失败，请查看运行日志。"})
            return self._json(404, {"error": "未找到"})

    return Handler


def serve(config: Path, state_dir: Path, port: int = 8765):
    """Serve only on loopback; closing this server stops its paper worker."""
    if not config.is_file():
        raise ValueError(f"配置文件不存在：{config}")
    manager = PaperProcess(config, state_dir)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler_for(manager, secrets.token_urlsafe(32)))
    server.daemon_threads = True
    print(f"本地模拟面板已启动，监听 127.0.0.1:{server.server_address[1]}；Ctrl+C 停止。", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        manager.stop()
