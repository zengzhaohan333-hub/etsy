"""Public-data comparison of fixed signals and shared-account position caps.

This module never loads credentials, talks to account endpoints, or changes the
running bot. Every scenario has one shared capital account for all four coins.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from decimal import Decimal, InvalidOperation
import hashlib
import json
from pathlib import Path
import sys
import time

from .backtest import _write_html
from .history import (DAY_MS, HistoryError, HistoryDataset, _validate,
                      fetch_dataset, load_dataset, save_dataset)
from .portfolio_config import DEFAULT_INSTRUMENTS, PortfolioConfig, load_portfolio_config
from .research_engine import run_portfolio_backtest
from .research_report import render_research_report
from .state import atomic_json, exclusive_lock


WARMUP_BARS = 720
BAR_MS = 300_000
CANDIDATE_DEFINITION = {
    "hourly_trend": "只使用完整收盘1H K线：SMA20/SMA50及小时收盘相对SMA50的同向过滤",
    "entry": "5m收盘突破此前20根高/低，排除当前根；仅新突破事件，不追过滤后才出现的旧事件",
    "atr": "ATR14 Wilder/RMA，初始14个真实波幅的算术均值",
    "stop": "max(1.5 × ATR14 / 信号收盘价, 0.5%)；原始距离超过3%时跳过",
    "take_profit": "止损距离的2倍；入场标记开盘价计算触发价，按价格粒度取整",
    "status": "研究候选，参数预先固定，尚未证明有效，不自动替换当前机器人",
}


class ResearchError(RuntimeError):
    """Fixed, non-secret setup or cache explanation."""


def _positive(value: str) -> Decimal:
    try:
        number = Decimal(value)
    except InvalidOperation:
        raise argparse.ArgumentTypeError("请输入正数") from None
    if not number.is_finite() or number <= 0 or number > Decimal("1e12"):
        raise argparse.ArgumentTypeError("请输入有效的有限正数")
    return number


def parser():
    result = argparse.ArgumentParser(description="四币策略研究：公开历史数据、共享资金，不读取账户或提交交易")
    result.add_argument("--config", type=Path, default=Path("config.portfolio.toml"))
    result.add_argument("--days", type=int, default=30)
    result.add_argument("--capital", type=_positive, default=Decimal("5000"))
    result.add_argument("--daily-target", type=_positive, default=Decimal("200"), help="报告比较阈值，不是收益保证")
    result.add_argument("--state-dir", type=Path, default=Path(".runtime-research"))
    result.add_argument("--dataset-dir", type=Path, help="离线加载四个已保存的合约JSON，不联网")
    result.add_argument("--max-total-notional", type=_positive, default=Decimal("1000"))
    return result


def _validate_datasets(datasets: dict[str, HistoryDataset], *, days=None):
    if set(datasets) != set(DEFAULT_INSTRUMENTS):
        raise ResearchError("研究需要 BTC、ETH、SOL、XRP 四个完整数据集。")
    boundaries = set()
    for instrument in DEFAULT_INSTRUMENTS:
        dataset = datasets[instrument]
        _validate(dataset)
        if dataset.instrument.inst_id != instrument or dataset.bar != "5m" or dataset.warmup_bars < WARMUP_BARS:
            raise ResearchError("研究数据的合约、5m周期或720根预热不匹配。")
        if days is not None and dataset.end_ms - dataset.start_ms != days * DAY_MS:
            raise ResearchError("数据时间长度与本次研究天数不匹配，请使用新的研究目录。")
        boundaries.add((dataset.start_ms, dataset.end_ms))
    if len(boundaries) != 1:
        raise ResearchError("四币数据必须使用同一历史区间，不能用错开的时间或合成数据补齐。")
    return next(iter(boundaries))


def build_research_report(profile: PortfolioConfig, datasets: dict[str, HistoryDataset], *,
                          capital: Decimal, daily_target: Decimal = Decimal("200"),
                          max_total_notional: Decimal = Decimal("1000"), source_files=None):
    for number in (capital, daily_target, max_total_notional):
        if not isinstance(number, Decimal) or not number.is_finite() or not 0 < number <= Decimal("1e12"):
            raise ResearchError("研究本金、比较阈值与名义总上限必须是有效正数。")
    if max_total_notional < Decimal("500"):
        raise ResearchError("500U单币方案要求合计名义上限至少500U。")
    start, end = _validate_datasets(datasets)
    count = (end - start) // BAR_MS
    split = start + int(Decimal(count) * Decimal("0.7")) * BAR_MS
    if split <= start or end - split < 2 * BAR_MS:
        raise ResearchError("研究区间不足以进行前后段比较。")
    cases = (("sma_200", "均线基线 · 单币200U", "sma_cross", Decimal("200")),
             ("sma_500", "均线基线 · 单币500U", "sma_cross", Decimal("500")),
             ("candidate_200", "趋势突破候选 · 单币200U", "trend_breakout_atr", Decimal("200")),
             ("candidate_500", "趋势突破候选 · 单币500U", "trend_breakout_atr", Decimal("500")))
    scenarios = []
    for identifier, label, strategy, cap in cases:
        settings = replace(profile, max_notional=cap, max_total_notional=max_total_notional)
        results = {}
        for segment, lower, upper, costs in (("full", start, end, Decimal(1)),
                                            ("train", start, split, Decimal(1)),
                                            ("test", split, end, Decimal(1)),
                                            ("test_double_costs", split, end, Decimal(2))):
            results[segment] = run_portfolio_backtest(settings, datasets, starting_equity=capital,
                start_ms=lower, end_ms=upper, strategy_name=strategy,
                fee_multiplier=costs, slippage_multiplier=costs, daily_target=daily_target)
        scenarios.append({"id": identifier, "label": label, "strategy": strategy,
                          "max_notional": str(cap), "max_total_notional": str(max_total_notional),
                          "results": results})
    research_profile = replace(profile, max_notional=Decimal("500"), max_total_notional=max_total_notional)
    settings = {key: str(value) if isinstance(value, Decimal) else value for key, value in asdict(research_profile).items()}
    warnings = [
        "本金是四币共享的单一虚拟账户，前后段各自独立重置；四份单币本金没有相加。",
        "四个方案共用合计名义上限；200U单币方案因四币各一仓，入场预算自然不超过800U。",
        "方案仅研究，不修改正在运行的参数，也不自动选择或上线测试段收益最高的方案。",
        "每日目标只作报告比较阈值，历史完整日达标比例不是未来概率，包含未实现盈亏的日收益也不是已兑现利润。",
        "按北京时间统计日收益；零交易日保留，不满完整一天的首尾区间单列，不计入达标比例。",
        "预先固定参数，后30%不参与选择或调参；后段双倍成本仅放大费用和滑点，不改变资金费记录。",
        "历史OHLC成交为近似，未知盘中路径按止损优先；未完整模拟市场深度、部分成交、清算或断网。",
        "采用当前合约规格与配置费用，可能不同于历史实际；数据SHA256仅检测缓存完整性，不是交易所签名。",
        "期末不强制平仓，报告保留未实现盈亏且不预扣未来平仓费用。",
        "30至60天只能初步筛查，不足以证明跨行情稳定；真实数据缺失时停止，不用演示行情代替。",
    ]
    return {"schema_version": 1, "mode": "research", "generated_at_ms": time.time_ns() // 1_000_000,
            "capital": str(capital), "daily_target": str(daily_target),
            "source": {"kind": "production_public", "instruments": list(DEFAULT_INSTRUMENTS), "bar": "5m",
                       "start_ms": start, "end_ms": end, "datasets": source_files or {}},
            "profile": settings, "split": {"ratio": "0.7", "train_end_ms": split},
            "candidate_definition": dict(CANDIDATE_DEFINITION), "scenarios": scenarios, "warnings": warnings}


def _state_directory(directory: Path):
    resolved = directory.resolve()
    if (resolved.name in {".runtime", ".runtime-demo", "okxbot"}
            or any((resolved / name).exists() for name in ("status.json", "demo-journal.json", "session.json"))):
        raise ResearchError("请使用独立研究目录，不能写入机器人状态目录。")


def summarize_report(report):
    """Keep all outcome/day metrics in a small shareable file, without curves."""
    summary = {key: value for key, value in report.items() if key != "scenarios"}
    summary["scenarios"] = []
    for scenario in report.get("scenarios", []):
        item = {key: value for key, value in scenario.items() if key != "results"}
        item["results"] = {
            segment: {key: value for key, value in result.items() if key not in {"equity_curve", "trades"}}
            for segment, result in scenario.get("results", {}).items()}
        summary["scenarios"].append(item)
    summary["detail_files"] = {"full_report": "report.json", "datasets": "datasets/*.json"}
    return summary


def _collect(profile, args):
    output = args.state_dir / "datasets"
    output.mkdir(parents=True, exist_ok=True)
    manifest_path = args.state_dir / "dataset_manifest.json"
    if args.dataset_dir is not None:
        datasets = {instrument: load_dataset(args.dataset_dir / (instrument + ".json"))
                    for instrument in DEFAULT_INSTRUMENTS}
        _validate_datasets(datasets, days=args.days)
    else:
        if manifest_path.exists():
            if manifest_path.stat().st_size > 64 * 1024:
                raise ResearchError("研究缓存清单无效，请使用新的研究目录。")
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            if (not isinstance(manifest, dict) or set(manifest) != {"version", "days", "end_ms", "warmup_bars"}
                    or type(manifest.get("version")) is not int or manifest["version"] != 1
                    or type(manifest.get("days")) is not int or manifest["days"] != args.days
                    or manifest.get("warmup_bars") != WARMUP_BARS
                    or type(manifest.get("end_ms")) is not int or manifest["end_ms"] % BAR_MS
                    or not 0 < manifest["end_ms"] <= time.time_ns() // 1_000_000):
                raise ResearchError("已有研究缓存区间与参数不符，请使用新的研究目录。")
            anchor = manifest["end_ms"]
        else:
            anchor = time.time_ns() // 1_000_000 // BAR_MS * BAR_MS
            atomic_json(manifest_path, {"version": 1, "days": args.days, "end_ms": anchor, "warmup_bars": WARMUP_BARS})
        datasets = {}
        for index, market in enumerate(profile.market_configs(), start=1):
            instrument = market.instrument
            cached = output / (instrument + ".json")
            print(f"[{index}/4] 正在校验或下载 {instrument} 的公开历史……", flush=True)
            if cached.exists():
                dataset = load_dataset(cached)
            else:
                dataset = fetch_dataset(market, days=args.days, now_ms=anchor, warmup_bars=WARMUP_BARS,
                                        progress=lambda message: print(message, flush=True),
                                        checkpoint_dir=args.state_dir / "downloads")
                save_dataset(dataset, cached)
            if (dataset.start_ms != anchor - args.days * DAY_MS or dataset.end_ms != anchor
                    or dataset.bar != "5m" or dataset.warmup_bars < WARMUP_BARS
                    or dataset.instrument.inst_id != instrument):
                raise ResearchError("已有合约缓存不属于本次共同时间区间，请使用新的研究目录。")
            datasets[instrument] = dataset
        _validate_datasets(datasets, days=args.days)
    sources = {}
    for instrument, dataset in datasets.items():
        path = output / (instrument + ".json")
        save_dataset(dataset, path)
        sources[instrument] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                               "retrieved_at_ms": dataset.retrieved_at_ms, "filename": "datasets/" + path.name}
    return datasets, sources


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if type(args.days) is not int or not 7 <= args.days <= 60:
            raise ResearchError("研究下载范围为7至60天，默认30天。")
        if args.max_total_notional < Decimal("500"):
            raise ResearchError("500U单币方案要求合计名义上限至少500U。")
        _state_directory(args.state_dir)
        profile = load_portfolio_config(args.config)
        print("四币共享资金研究：仅公开历史数据，不需要API密钥，不会下单或修改当前机器人。", flush=True)
        print(f"本次研究本金{args.capital}U，每日{args.daily_target}U只作比较阈值；"
              f"每币200/500U，四币合计{args.max_total_notional}U。", flush=True)
        print("历史分页校验后逐页保存；重开此研究入口会复核断点并续传，无需重启交易机器人。", flush=True)
        with exclusive_lock(args.state_dir / "research.lock"):
            datasets, sources = _collect(profile, args)
            print("四币数据已对齐，正在比较固定策略、仓位和双倍成本场景……", flush=True)
            report = build_research_report(profile, datasets, capital=args.capital, daily_target=args.daily_target,
                                           max_total_notional=args.max_total_notional, source_files=sources)
            atomic_json(args.state_dir / "report.json", report)
            atomic_json(args.state_dir / "summary.json", summarize_report(report))
            _write_html(args.state_dir / "report.html", render_research_report(report))
        print(f"研究报告已生成：{args.state_dir / 'report.html'}；没有修改任何交易配置。", flush=True)
        print("需要分析结果时，可提供同目录 summary.json；完整曲线和交易仍保留在 report.json。", flush=True)
        return 0
    except (ResearchError, HistoryError) as error:
        print(f"研究未完成：{error}", file=sys.stderr)
        if isinstance(error, HistoryError):
            print("已通过校验的分页保留在研究目录；请保留该目录，按上方原因处理后重新打开研究入口续传。",
                  file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("研究已取消；已保存的分页和完整合约缓存可在下次继续使用。", file=sys.stderr)
        return 130
    except Exception:
        print("研究未完成，请检查历史连接、缓存和配置；原始错误未显示，不会替换为演示数据。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
