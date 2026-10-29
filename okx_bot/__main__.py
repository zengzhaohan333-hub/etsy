"""Command line: offline first, demo orders require a separate explicit flag."""
from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
from pathlib import Path

from .config import load_config
from .okx import Credentials, OkxClient, OkxError


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="OKX 合约机器人：本地模拟 / 欧易模拟盘，无实盘功能")
    sub = root.add_subparsers(dest="command", required=True)
    for command, help_text in (("paper", "使用内置合成行情，离线模拟"),
                               ("web", "启动中文本地模拟面板"),
                               ("market", "只读获取 OKX 公开行情"),
                               ("demo-check", "只读检查 OKX 模拟账户，不下单"),
                               ("demo-reconcile", "只读核对模拟盘状态，满足条件后更新本地恢复日志"),
                               ("demo", "运行 OKX 模拟盘，需要模拟盘凭证与明确开关")):
        p = sub.add_parser(command, help=help_text)
        p.add_argument("--config", type=Path, default=Path("config.paper.toml"))
        p.add_argument("--state-dir", type=Path,
                       default=Path(".runtime-demo" if command.startswith("demo") else ".runtime"))
        if command == "paper":
            p.add_argument("--bars", type=int, default=320)
            p.add_argument("--interval", type=float, default=0)
        if command == "web":
            p.add_argument("--port", type=int, default=8765)
        if command == "demo":
            p.add_argument("--ack-demo-orders", action="store_true",
                           help="允许在 OKX 模拟盘下单；始终发送 x-simulated-trading: 1")
            p.add_argument("--poll-seconds", type=float, default=15)
        if command == "demo-reconcile":
            p.add_argument("--ack-journal", action="store_true", help="核对交易所状态后允许更新本地订单日志")
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = load_config(args.config)
        if args.command == "web":
            from .web import serve
            serve(args.config, args.state_dir, args.port)
            return 0
        if args.command == "market":
            client = OkxClient()
            instrument = client.instrument(config.instrument)
            candles = client.candles(config.instrument, config.bar, config.slow_window + 2)
            if not candles:
                raise OkxError("没有已收盘 K 线")
            print(json.dumps({"instrument": instrument.inst_id, "ct_val": str(instrument.ct_val),
                              "closed_bars": len(candles), "latest_close": str(candles[-1].close),
                              "latest_bar": candles[-1].ts}, ensure_ascii=False))
            return 0
        if args.command == "demo-check":
            client = OkxClient(Credentials.from_env())
            account = client.account_config()
            balance = client.balances()
            print(json.dumps({"mode": "demo", "position_mode": account.get("posMode"),
                              "account_level": account.get("acctLv"), "balance": balance,
                              "positions": len(client.positions(config.instrument)),
                              "pending_orders": len(client.pending_orders(config.instrument)),
                              "pending_algos": len(client.pending_algos(config.instrument))},
                             ensure_ascii=False, default=str))
            return 0
        if args.command == "demo" and not args.ack_demo_orders:
            raise ValueError("模拟盘下单需要 --ack-demo-orders；实盘功能未实现")
        if args.command == "demo-reconcile":
            if not args.ack_journal:
                raise ValueError("恢复核对需要 --ack-journal；不会向交易所下单或撤单")
            from .demo import reconcile_demo
            result = reconcile_demo(config, args.state_dir)
            print(json.dumps(result, ensure_ascii=False, default=str))
            return 1 if result.get("halted") else 0
        stop = threading.Event()
        def request_stop(_signal, _frame):
            stop.set()
        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
        if args.command == "paper":
            from .paper import run_paper
            result = run_paper(config, args.state_dir, args.bars, args.interval, stop)
            brief = {k: result[k] for k in ("mode", "feed", "processed_bars", "total_trades",
                                           "equity", "fees", "halted", "halt_reason")}
            print(json.dumps(brief, ensure_ascii=False))
            return 0
        from .demo import run_demo
        result = run_demo(config, args.state_dir, stop, poll_seconds=args.poll_seconds)
        print(json.dumps(result, ensure_ascii=False, default=str))
        return 1 if result.get("halted") else 0
    except (OSError, ValueError, RuntimeError) as exc:
        # Adapter exceptions are sanitized. Do not log request bodies or headers.
        print(f"无法继续：{exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
