"""Offline development replay of one frozen turnover/cost-control hypothesis.

The previous 30-day results have already informed this experiment. Reusing that
history is development, including the former final 30%; it cannot validate a
new strategy out of sample. No downloads, account access or bot controls exist
in this entry point.
"""
from __future__ import annotations

import argparse
from dataclasses import asdict, replace
from datetime import datetime
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
import time

from .backtest import _write_html
from .history import DAY_MS, HistoryDataset, HistoryError, load_dataset, save_dataset
from .portfolio_config import DEFAULT_INSTRUMENTS, PortfolioConfig, load_portfolio_config
from .research import (BAR_MS, CANDIDATE_DEFINITION, ResearchError, _positive,
                       _state_directory, _validate_datasets, summarize_report)
from .research_engine import run_portfolio_backtest
from .research_low_turnover_signals import RULES
from .research_report import render_research_report
from .state import atomic_json, exclusive_lock


D = Decimal
RULES_FROZEN_AT = "2026-10-30T15:19:06+00:00"
RULES_FROZEN_AT_MS = int(datetime.fromisoformat(RULES_FROZEN_AT).timestamp()) * 1000
EXPERIMENT_ID = "low_turnover_v2"
PER_COIN_CAP = D("200")
TOTAL_CAP = D("1000")
LOW_TURNOVER_DEFINITION = {
    **CANDIDATE_DEFINITION,
    "hourly_slope": "完整小时SMA20相对3小时前同向上升/下降，仍须满足原SMA20/50趋势过滤",
    "breakout_buffer": "原始新突破事件还须超出此前20根高/低至少0.25倍当前已收盘ATR14",
    "cost_filter": "估算净收益风险比至少1.5；往返摩擦=2×(手续费率+滑点率)，净收益=止盈比例-摩擦，净风险=止损比例+摩擦",
    "entry_throttle": "仅成功模拟入场占名额：同币入场间隔至少6小时，北京时间每天最多2次；持仓、预算不足和未成交信号不占名额",
    "stress_cost": "双倍成本时用放大后的实际假设费用与滑点重新过滤，交易数量可能变化",
    "status": "已见历史上的开发假设；本轮参数固定，不搜索、不自动选择，尚未通过新的独立区间或后续模拟验证",
}


def parser():
    result = argparse.ArgumentParser(description="低换手开发复盘：复用四币公开缓存，离线运行，不修改交易机器人")
    result.add_argument("--dataset-dir", type=Path, required=True,
                        help="上一轮datasets目录，须含BTC/ETH/SOL/XRP四个完整JSON")
    result.add_argument("--config", type=Path, default=Path("config.portfolio.toml"))
    result.add_argument("--capital", type=_positive, default=D("5000"))
    result.add_argument("--daily-target", type=_positive, default=D("200"), help="仅历史观察阈值，不是盈利要求或保证")
    result.add_argument("--state-dir", type=Path, default=Path(".runtime-research-v2"))
    return result


def build_low_turnover_report(profile: PortfolioConfig, datasets: dict[str, HistoryDataset], *,
                              capital: Decimal, daily_target: Decimal = D("200"), source_files=None):
    if any(not isinstance(value, Decimal) or not value.is_finite() or not 0 < value <= D("1e12")
           for value in (capital, daily_target)):
        raise ResearchError("虚拟本金和观察阈值必须是有效正数。")
    start, end = _validate_datasets(datasets)
    if not 7 * DAY_MS <= end - start <= 60 * DAY_MS:
        raise ResearchError("本轮复盘仅接受7至60天的完整四币历史缓存。")
    split = start + int(D((end - start) // BAR_MS) * D("0.7")) * BAR_MS
    settings = replace(profile, max_notional=PER_COIN_CAP, max_total_notional=TOTAL_CAP)
    cases = (("sma_200", "原均线基准 · 单币200U", "sma_cross"),
             ("candidate_200", "上一轮突破候选 · 单币200U", "trend_breakout_atr"),
             ("low_turnover_200", "低换手开发候选 · 单币200U", "low_turnover_v2"))
    scenarios = []
    for identifier, label, strategy in cases:
        results = {}
        for segment, lower, upper, costs in (("full", start, end, D(1)),
                                            ("train", start, split, D(1)),
                                            ("test", split, end, D(1)),
                                            ("test_double_costs", split, end, D(2))):
            results[segment] = run_portfolio_backtest(settings, datasets, starting_equity=capital,
                start_ms=lower, end_ms=upper, strategy_name=strategy,
                fee_multiplier=costs, slippage_multiplier=costs, daily_target=daily_target)
        scenarios.append({"id": identifier, "label": label, "strategy": strategy,
                          "max_notional": str(PER_COIN_CAP), "max_total_notional": str(TOTAL_CAP),
                          "results": results})
    encoded_rules = json.dumps({"definition": LOW_TURNOVER_DEFINITION, "rules": dict(RULES)},
                               sort_keys=True, separators=(",", ":"),
                               ensure_ascii=True).encode("utf-8")
    return {"schema_version": 1, "mode": "research", "experiment_id": EXPERIMENT_ID,
            "generated_at_ms": time.time_ns() // 1_000_000,
            "capital": str(capital), "daily_target": str(daily_target),
            "source": {"kind": "production_public", "instruments": list(DEFAULT_INSTRUMENTS),
                       "bar": "5m", "start_ms": start, "end_ms": end, "datasets": source_files or {}},
            "profile": {key: str(value) if isinstance(value, Decimal) else value
                        for key, value in asdict(settings).items()},
            "split": {"ratio": "0.7", "train_end_ms": split},
            "validation": {"status": "development_seen",
                           "label": "开发复盘：历史区间已用于策略设计，后30%不再是独立样本外验证",
                           "frozen_at_ms": RULES_FROZEN_AT_MS,
                           "rules": dict(RULES),
                           "rules_sha256": hashlib.sha256(encoded_rules).hexdigest(),
                           "unseen_validation_required": True,
                           "deployment_ready": False},
            "candidate_definition": dict(LOW_TURNOVER_DEFINITION), "scenarios": scenarios,
            "warnings": [
                "本轮复用已看过的历史；全区间及前后段都只能用于开发复盘，不能将后30%称为新样本外验证。",
                "增加过滤和冷却是降低换手的待检验假设，不保证改善盈利；本轮没有参数搜索或自动选赢家。",
                "每段四币共享一份虚拟本金，前后段独立重置，不能相加；实际本金尚未确认。",
                "三方案均保持单币200U；合计预算上限1000U，但四币各一仓的200U上限合计至多800U，且可能更小。",
                "每日目标仅为历史观察阈值，不驱动开仓或增加金额，历史达标比例不是未来概率。",
                "低换手候选四币每天最多8次新入场；按每笔200U且2%毛收益举例也只有32U，未计亏损和费用，不代表日盈亏上限或收益预测。",
                "北京时间自然日计入零交易日；首尾不完整日单列，不计入完整日平均与达标比例。",
                "双倍成本放大手续费和滑点，候选成本门槛也使用放大后的成本，因此实际入场数量可能变化。",
                "成本门槛是按预设保护比例估算的过滤值，价格粒度、标记/成交价基差与跳空可能使实际成交收益风险比不同。",
                "成本过滤可能产生很少或零交易，不能把没有交易的压力结果当作盈利稳健的证明。",
                "沿用OHLC近似、标记价保护、实际历史资金费及共享回撤闸门；无法完整复现盘口、部分成交、延迟、断网或强平。",
                "期末保留仓位，以最后标记价计入未实现盈亏，未预扣未来平仓费；采用当前合约规格和假设费用。",
                "缓存摘要只校验完整性，不是交易所签名；数据缺失或不对齐就停止，不下载或合成替代数据。",
                "即使开发结果变好，也须冻结规则后用新的未见区间及后续模拟验证；报告不会部署或替换正在运行的策略。",
            ]}


def _load_sources(directory: Path):
    datasets = {symbol: load_dataset(directory / (symbol + ".json")) for symbol in DEFAULT_INSTRUMENTS}
    _validate_datasets(datasets)
    return datasets


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        _state_directory(args.state_dir)
        source, target = args.dataset_dir.resolve(), args.state_dir.resolve()
        if source == target or source in target.parents or target in source.parents:
            raise ResearchError("请使用独立结果目录，不能覆盖或嵌套在原始历史缓存目录中。")
        profile = load_portfolio_config(args.config)
        print("低换手开发复盘：仅复用上一轮公开缓存，无需代理、网络或API密钥。", flush=True)
        print("这段历史已经看过，前后段均属开发资料；结果不能当成新的样本外收益。", flush=True)
        print("三方案均为单币200U，虚拟本金共享；每日200U仅作比较，不承诺、不强迫下单。", flush=True)
        with exclusive_lock(args.state_dir / "research.lock"):
            datasets = _load_sources(args.dataset_dir)
            source_files = {}
            for symbol, dataset in datasets.items():
                path = args.state_dir / "datasets" / (symbol + ".json")
                save_dataset(dataset, path)
                source_files[symbol] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                        "retrieved_at_ms": dataset.retrieved_at_ms,
                                        "filename": "datasets/" + path.name}
            print("四币缓存完整且时间对齐，正在复盘基准、上一轮候选和一个固定低换手假设……", flush=True)
            report = build_low_turnover_report(profile, datasets, capital=args.capital,
                                               daily_target=args.daily_target, source_files=source_files)
            atomic_json(args.state_dir / "report.json", report)
            atomic_json(args.state_dir / "summary.json", summarize_report(report))
            _write_html(args.state_dir / "report.html", render_research_report(report))
        print(f"开发报告已生成：{args.state_dir / 'report.html'}；请提供同目录summary.json分析。", flush=True)
        print("交易机器人、现有仓位、额度和交易日志均未修改；本轮未证明每日盈利200U。", flush=True)
        return 0
    except (ResearchError, HistoryError) as error:
        print(f"开发复盘未完成：{error}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("开发复盘已取消；原始历史和机器人状态保持不变。", file=sys.stderr)
        return 130
    except Exception:
        print("开发复盘未完成，请检查是否选中完整四币datasets目录及可写结果目录；原始异常未显示。",
              file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
