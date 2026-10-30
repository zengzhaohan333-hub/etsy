"""Read-only historical validation. This module never creates an OKX order."""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
import tempfile
import time
from dataclasses import asdict
from decimal import Decimal, InvalidOperation
from pathlib import Path

from .backtest_engine import SUPPORTED_BARS, run_backtest
from .backtest_report import render_report
from .config import Config, load_config
from .core import bar_milliseconds
from .history import HistoryDataset, HistoryError, fetch_dataset, load_dataset, save_dataset
from .state import atomic_json, exclusive_lock


class BacktestSetupError(RuntimeError):
    """A local, credential-free explanation of refused backtest settings."""


def _capital(value: str) -> Decimal:
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("虚拟本金必须为正数") from None
    if not amount.is_finite() or amount <= 0:
        raise argparse.ArgumentTypeError("虚拟本金必须为有限正数")
    return amount


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="固定策略真实历史回测：仅公开数据，不需要密钥，不下单")
    result.add_argument("--config", type=Path, default=Path("config.paper.toml"))
    result.add_argument("--days", type=int, default=30, help="下载最近 7–60 天，默认 30 天")
    result.add_argument("--capital", type=_capital, default=Decimal("5000"), help="各段独立虚拟本金，默认 5000 USDT")
    result.add_argument("--state-dir", type=Path, default=Path(".runtime-backtest"))
    result.add_argument("--dataset", type=Path, help="离线重放先前保存的 dataset.json，不联网")
    return result


def _validate_setup(config: Config, days: int, state_dir: Path) -> None:
    if config.bar not in SUPPORTED_BARS:
        raise BacktestSetupError("本次历史验证仅支持 5m 和 15m 周期。其他周期尚未验证。")
    if type(days) is not int or not 7 <= days <= 60:
        raise BacktestSetupError("历史下载范围必须是 7–60 天；默认 30 天。")
    if state_dir.resolve().name in {".runtime", ".runtime-demo"} or (state_dir / "demo-journal.json").exists():
        raise BacktestSetupError("请使用独立的回测目录，例如 .runtime-backtest，保留模拟盘和本地演示的状态目录。")


def _snapshot_config(config: Config) -> dict:
    return {name: str(value) if isinstance(value, Decimal) else value
            for name, value in asdict(config).items()}


def build_report(config: Config, dataset: HistoryDataset, capital: Decimal,
                 dataset_sha256: str) -> dict:
    """Keep the predeclared parameters fixed for all chronological segments."""
    if config.bar not in SUPPORTED_BARS:
        raise BacktestSetupError("本次历史验证仅支持 5m 和 15m 周期。其他周期尚未验证。")
    if dataset.bar != config.bar or dataset.instrument.inst_id != config.instrument:
        raise BacktestSetupError("历史数据的合约或周期与当前策略配置不一致。")
    if dataset.warmup_bars < config.slow_window + 1:
        raise BacktestSetupError("历史数据的预热长度不足；请重新下载与此策略匹配的数据。")
    interval = bar_milliseconds(config.bar)
    count = (dataset.end_ms - dataset.start_ms) // interval
    if count < 70:
        raise BacktestSetupError("可测量的历史区间不足，无法进行前后段验证。")
    split_ms = dataset.start_ms + int(Decimal(count) * Decimal("0.7")) * interval

    def run(start: int, end: int, *, costs: Decimal = Decimal("1")) -> dict:
        return run_backtest(config, dataset.trade_candles, dataset.mark_candles,
            [(ts, rate) for ts, rate in dataset.funding_rates if start <= ts < end],
            dataset.instrument, starting_equity=capital, start_ms=start, end_ms=end,
            fee_multiplier=costs, slippage_multiplier=costs)

    results = {
        "full": run(dataset.start_ms, dataset.end_ms),
        "train": run(dataset.start_ms, split_ms),
        "test": run(split_ms, dataset.end_ms),
        "test_double_costs": run(split_ms, dataset.end_ms, costs=Decimal("2")),
    }
    warnings = [
        "策略参数保持固定；前 70% 与后 30% 按时间划分，后段独立重置虚拟本金，不继承前段仓位。",
        "生产环境公开历史行情用于研究；欧易模拟盘的行情、成交与真实资金市场可能不同。",
        "使用当前合约规格快照，未重建历史规格变更；手续费是配置假设，并非账户实际费率。",
        f"{interval // 60_000} 分钟 OHLC 无法恢复盘中路径与盘口，标记价触发后的成交价格为近似；不能验证实盘可成交性。",
        "回撤按 K 线开收盘及成交事件采样，可能遗漏盘中波动；未完整模拟清算、部分成交、延迟及断网。",
        "资金费覆盖按结算记录与不超过 8 小时间隔检查，无法据此证明所有历史结算记录均未遗漏。",
        "期末不强制平仓；总收益可能含未实现盈亏，尚未产生的平仓手续费没有计入。",
        "这份报告用于检验固定规则，不证明未来收益，也不依据后段结果自动调参。",
    ]
    if results["test"]["closed_trades"] < 30:
        warnings.append("后段已平仓交易少于 30 笔，样本较少，无法据此判断策略稳定性；30 笔也不代表统计保证。")
    for key, label in (("full", "全区间"), ("train", "前段"),
                       ("test", "后段"), ("test_double_costs", "后段双倍成本")):
        if results[key]["halted"]:
            warnings.append(f"{label}触发回撤停止：其后不再开仓，但已有保护单与资金费继续模拟。")
    return {
        "schema_version": 1, "generated_at_ms": time.time_ns() // 1_000_000,
        "source": {"exchange": "OKX", "kind": "production_public",
                   "instrument": config.instrument, "bar": config.bar,
                   "start_ms": dataset.start_ms, "end_ms": dataset.end_ms,
                   "retrieved_at_ms": dataset.retrieved_at_ms},
        "config": _snapshot_config(config),
        "split": {"ratio": "0.7", "train_end_ms": split_ms},
        "results": results, "warnings": warnings, "dataset_sha256": dataset_sha256,
    }


def _write_html(path: Path, content: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=".report-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        config = load_config(args.config)
        _validate_setup(config, args.days, args.state_dir)
        print("固定策略历史回测：只读取公开数据，不需要 API 密钥，不向交易所下单。", flush=True)
        print("本次回测不修改正在运行的欧易模拟盘机器人或策略参数。", flush=True)
        with exclusive_lock(args.state_dir / "backtest.lock"):
            if args.dataset is not None:
                print("正在校验并重放本地历史数据……", flush=True)
                dataset = load_dataset(args.dataset)
            else:
                dataset = fetch_dataset(config, days=args.days, progress=lambda message: print(message, flush=True))
            dataset_path = args.state_dir / "dataset.json"
            save_dataset(dataset, dataset_path)
            dataset_hash = hashlib.sha256(dataset_path.read_bytes()).hexdigest()
            print("历史数据校验完成。正在回放全区间、前后段和双倍成本场景……", flush=True)
            report = build_report(config, dataset, args.capital, dataset_hash)
            atomic_json(args.state_dir / "report.json", report)
            _write_html(args.state_dir / "report.html", render_report(report))
            test = report["results"]["test"]
            print(f"回测完成。后段净收益 {test['net_pnl']} USDT；"
                  f"最大回撤 {test['max_drawdown_pct']}%；已平仓 {test['closed_trades']} 笔。", flush=True)
            print(f"报告：{args.state_dir / 'report.html'}", flush=True)
            print("数据与 JSON 报告已保存在同一目录。请先阅读假设和样本限制，再评估策略。", flush=True)
        return 0
    except (HistoryError, BacktestSetupError) as error:
        print(f"回测未完成：{error}", file=sys.stderr)
        return 1
    except (EOFError, KeyboardInterrupt):
        print("回测已取消，不影响欧易模拟盘机器人。", file=sys.stderr)
        return 130
    except Exception:
        # Transport exceptions can contain proxy URLs; never print raw details.
        print("回测未完成。请检查公开行情连接、配置和数据格式；同一回测目录请只运行一个进程。"
              "本次没有向交易所下单，也没有修改模拟盘机器人。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
