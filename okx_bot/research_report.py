"""Passive HTML presentation of offline portfolio research scenarios.

The report displays supplied observations; it never ranks strategies, infers a
future return, downloads market data or changes a running robot's configuration.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html import escape
import json
from typing import Any


_ZONE = timezone(timedelta(hours=8))
_SEGMENTS = (("full", "全样本"), ("train", "前 70%"), ("test", "后 30%"),
             ("test_double_costs", "后段 · 双倍手续费与滑点"))
_DEVELOPMENT_SEGMENTS = (("full", "全区间开发复盘"), ("train", "前70%开发复盘"),
                         ("test", "后30%开发复盘"),
                         ("test_double_costs", "后段双倍成本开发压力"))
_COLORS = ("#6ce2b3", "#81b9ff", "#f4be77", "#c3a0ff")


def _e(value: Any) -> str:
    return escape(str(value), quote=True)


def _obj(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return number if number.is_finite() and number.copy_abs() <= Decimal("1e30") else None


def _fmt(value: Any, digits: int = 2) -> str:
    number = _number(value)
    return f"{number:,.{digits}f}" if number is not None else "—"


def _pct(value: Any) -> str:
    return _fmt(value) + "%" if _number(value) is not None else "—"


def _time(value: Any) -> str:
    number = _number(value)
    if number is None:
        return "未提供"
    try:
        return datetime.fromtimestamp(float(number / 1000), _ZONE).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, OSError, ValueError):
        return "时间无效"


def _tone(value: Any) -> str:
    number = _number(value)
    return "positive" if number is not None and number > 0 else "negative" if number is not None and number < 0 else ""


def _strategy(value: Any) -> str:
    # Unknown names are retained as escaped text rather than relabeled as a
    # strategy that this renderer cannot verify.
    names = {"sma": "原均线交叉", "sma_cross": "原均线交叉", "baseline": "原均线交叉",
             "candidate": "候选趋势突破 ATR", "trend_breakout_atr": "候选趋势突破 ATR",
             "low_turnover_v2": "低频候选 V2（待验证）"}
    return names.get(value, str(value)) if isinstance(value, str) else "策略未提供"


def _positions(result: dict) -> str:
    positions = result.get("open_positions")
    if not isinstance(positions, dict):
        return "未提供"
    if not positions:
        return "无"
    names = " / ".join(str(name) for name in positions)
    return _e(f"{len(positions)} 个未平仓") + "<small>" + _e(names) + "</small>"


def _halt(result: dict) -> str:
    if result.get("halted") is True:
        return '<span class="warning">已停新增</span><small>' + _e(_time(result.get("halted_at_ms"))) + "</small>"
    return "未触发" if result.get("halted") is False else "未提供"


def _budget_text(report: dict, scenarios: list[dict], instruments: Any) -> str:
    """Describe entry constraints from this report, without a default cap.

    A cap is checked before entry. Mark-price drift after a fill can exceed it;
    it is not a guarantee of the maximum observed exposure during a position.
    """
    totals = [_number(item.get("max_total_notional")) for item in scenarios]
    valid_totals = [value for value in totals if value is not None and value > 0]
    profile_cap = _number(_obj(report.get("profile")).get("max_total_notional"))
    common = None
    if totals and len(valid_totals) == len(totals) and len(set(valid_totals)) == 1:
        common = valid_totals[0]
        description = "各场景的组合名义入场预算上限共同为 " + _fmt(common) + " USDT。"
    elif not valid_totals and profile_cap is not None and profile_cap > 0:
        common = profile_cap
        description = "研究配置中的组合名义入场预算上限为 " + _fmt(common) + " USDT；各场景以其记录为准。"
    else:
        description = "组合名义入场预算上限按各场景分别列示，不能假定各场景共用一个固定金额。"
    count = len(set(instruments)) if isinstance(instruments, list) and instruments and all(isinstance(value, str) for value in instruments) else None
    per_market = sorted({value for value in (_number(item.get("max_notional")) for item in scenarios)
                         if value is not None and value > 0})
    if count is not None and common is not None:
        for cap in per_market:
            per_coin_sum = cap * count
            # Integer formatting keeps the 4×200 budget arithmetic readable;
            # noninteger overrides still retain their fractional precision.
            digits = 0 if cap == cap.to_integral_value() and common == common.to_integral_value() else 2
            description += (" 单币 " + _fmt(cap, digits) + " USDT 时，各币上限合计为 "
                            + str(count) + " × " + _fmt(cap, digits) + " = " + _fmt(per_coin_sum, digits)
                            + " USDT；与组合上限共同约束后，合计入场预算至多 "
                            + _fmt(min(per_coin_sum, common), digits) + " USDT。")
    return description + " 这些上限仅约束新增入场；持仓后标记价格变化可能使名义敞口超过预算，系统不会因此自动减仓。实际开仓金额还可能因风险预算和合约粒度更小。这些是研究约束，不是资金配置建议。"


def _metrics(results: dict, segments: tuple = _SEGMENTS) -> str:
    rows = []
    for key, label in segments:
        result = _obj(results.get(key))
        if not result:
            rows.append('<tr><th scope="row">' + _e(label) + '</th><td colspan="9" class="muted">没有可用结果，不能判断策略表现</td></tr>')
            continue
        factor = result.get("profit_factor")
        factor_text = _fmt(factor) if factor is not None else "—（无定义）"
        win_rate = result.get("win_rate_pct", result.get("win_rate"))
        rows.append('<tr><th scope="row">' + _e(label) + '<small>' + _e(_time(result.get("start_ms"))) + '<br>至 ' + _e(_time(result.get("end_ms"))) + '</small></th>'
                    + '<td class="' + _tone(result.get("net_pnl")) + '">' + _fmt(result.get("net_pnl"))
                    + '<small>' + _pct(result.get("return_pct")) + '</small></td><td>' + _pct(result.get("max_drawdown_pct"))
                    + '</td><td>' + _fmt(result.get("entries"), 0) + ' / ' + _fmt(result.get("closed_trades"), 0)
                    + '</td><td>' + _pct(win_rate) + '<small>盈利因子 ' + factor_text + '</small></td><td>' + _fmt(result.get("fees"))
                    + '</td><td class="' + _tone(result.get("funding_pnl")) + '">' + _fmt(result.get("funding_pnl"))
                    + '</td><td>' + _fmt(result.get("ending_equity")) + '</td><td>' + _positions(result)
                    + '</td><td>' + _halt(result) + '</td></tr>')
    return '<div class="scroll"><table class="metrics-table"><thead><tr><th>时间段 · UTC+8</th><th>净收益 / USDT<small>收益率</small></th><th>最大回撤</th><th>入场 / 平仓</th><th>胜率 / 盈利因子</th><th>手续费 / USDT</th><th>资金费盈亏 / USDT</th><th>期末权益 / USDT</th><th>期末持仓</th><th>回撤停止新增</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>'


def _daily(results: dict, segments: tuple = _SEGMENTS) -> str:
    rows = []
    for key, label in segments:
        stats = _obj(_obj(results.get(key)).get("daily_stats"))
        if not stats:
            rows.append('<tr><th scope="row">' + _e(label) + '</th><td colspan="7" class="muted">没有完整日统计，不能推断每日目标达成能力</td></tr>')
            continue
        days = _number(stats.get("complete_days"))
        has_days = days is not None and days > 0
        ratio = _pct(stats.get("target_day_ratio_pct")) if has_days else "—（无完整日）"
        rows.append('<tr><th scope="row">' + _e(label) + '</th><td>' + _fmt(stats.get("complete_days"), 0)
                    + '</td><td>' + _fmt(stats.get("partial_days"), 0)
                    + '</td><td class="' + _tone(stats.get("mean_net_pnl") if has_days else None) + '">' + (_fmt(stats.get("mean_net_pnl")) if has_days else "—")
                    + '</td><td>' + (_fmt(stats.get("median_net_pnl")) if has_days else "—")
                    + '</td><td>' + _fmt(stats.get("loss_days"), 0)
                    + '</td><td>' + _fmt(stats.get("target_days"), 0) + '</td><td>' + ratio + '</td></tr>')
    return '<div class="scroll"><table class="daily-table"><thead><tr><th>时间段</th><th>完整日数</th><th>不完整日数</th><th>历史日均净盈亏 / USDT</th><th>历史日中位净盈亏 / USDT</th><th>亏损日数</th><th>历史达标日数</th><th>历史完整日达标比例<small>非预测</small></th></tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>'


def _chart(scenarios: list[dict], development: bool = False) -> str:
    series = []
    for scenario in scenarios:
        curve = _obj(_obj(scenario.get("results")).get("test")).get("equity_curve")
        if not isinstance(curve, list) or len(curve) < 2:
            continue
        # Sampling is only for display. Metrics remain the runner's full-series
        # results and are never calculated from the chart's reduced points.
        selected = curve if len(curve) <= 400 else [curve[index * (len(curve) - 1) // 399] for index in range(400)]
        points = []
        for raw in selected:
            row = _obj(raw)
            ts, equity = _number(row.get("ts")), _number(row.get("equity"))
            if ts is not None and equity is not None:
                points.append((ts, equity))
        points.sort(key=lambda point: point[0])
        if len(points) >= 2:
            series.append((str(scenario.get("label", scenario.get("id", "未命名场景"))), points))
    if not series:
        return '<div class="empty">后段权益序列不足，未绘制比较曲线。</div>'
    first = min(points[0][0] for _, points in series)
    last = max(points[-1][0] for _, points in series)
    low = min(value for _, points in series for _, value in points)
    high = max(value for _, points in series for _, value in points)
    padding = max((high - low) / 10, Decimal("0.1"))
    low, high = low - padding, high + padding
    span = max(Decimal(1), last - first)
    lines, legend = [], []
    for index, (label, points) in enumerate(series):
        color = _COLORS[index % len(_COLORS)]
        coords = " ".join(f"{75 + (ts - first) / span * 825:.2f},{235 - (value - low) / (high - low) * 210:.2f}" for ts, value in points)
        lines.append('<polyline fill="none" stroke="' + color + '" stroke-width="2" vector-effect="non-scaling-stroke" points="' + coords + '"/>')
        legend.append('<span><i style="background:' + color + '"></i>' + _e(label) + '</span>')
    grid = []
    for index in range(4):
        y = 235 - Decimal(index) * 70
        value = low + (high - low) * Decimal(index) / 3
        grid.append(f'<line class="gridline" x1="75" x2="900" y1="{y}" y2="{y}"/><text x="65" y="{y + 4}" text-anchor="end">{_fmt(value)}</text>')
    title = "后段开发复盘共享账户权益比较" if development else "后段共享账户权益比较"
    aria_label = "后段开发复盘共享账户权益曲线" if development else "研究场景后段共享账户权益曲线"
    return '<div class="legend">' + "".join(legend) + '</div><svg viewBox="0 0 930 280" role="img" aria-label="' + _e(aria_label) + '"><title>' + _e(title) + '，单位 USDT；图形抽样显示</title>' + "".join(grid) + "".join(lines) + '<text x="75" y="268">' + _e(_time(first)) + '</text><text x="900" y="268" text-anchor="end">' + _e(_time(last)) + '</text></svg>'


def _source(source: dict) -> str:
    datasets = _obj(source.get("datasets"))
    rows = []
    for instrument, value in datasets.items():
        item = _obj(value)
        rows.append('<tr><th scope="row">' + _e(instrument) + '</th><td>' + _e(item.get("filename", "未提供"))
                    + '</td><td>' + _e(_time(item.get("retrieved_at_ms"))) + '</td><td class="hash">' + _e(item.get("sha256", "未提供")) + '</td></tr>')
    if not rows:
        return '<div class="empty">数据来源元信息未提供，不能确认本次历史样本。</div>'
    return '<div class="scroll"><table><thead><tr><th>合约</th><th>缓存文件</th><th>获取时间 · UTC+8</th><th>数据集 SHA-256</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>'


def render_research_report(report: dict) -> str:
    """Render a static comparison without selecting a winner or placing orders."""
    source, split = _obj(report.get("source")), _obj(report.get("split"))
    validation = _obj(report.get("validation"))
    development = validation.get("status") == "development_seen"
    segments = _DEVELOPMENT_SEGMENTS if development else _SEGMENTS
    title = "四币组合 · 开发复盘对照" if development else "四币组合 · 离线研究对照"
    subtitle = "已见历史区间上的开发诊断，保留负收益、成本与未平仓情况" if development else "固定规则与仓位约束对照，保留负收益、成本与未平仓情况"
    badge = "已见历史开发复盘 · 待独立验证" if development else "候选策略未验证 · 不启动交易"
    validation_html = ""
    if development:
        label = validation.get("label")
        label = label if isinstance(label, str) and label.strip() else "开发复盘：历史区间已用于策略设计，后30%不再是独立样本外验证"
        required = "仍须用规则冻结后、此前未查看的新数据或后续模拟记录进行独立验证。" if validation.get("unseen_validation_required") is True else "本报告未提供此前未查看的新数据，不能作为独立验证。"
        validation_html = ('<section class="panel notice validation"><h2>开发复盘 · 已见历史数据</h2><strong>'
                           + _e(label) + '</strong><p>全区间、前段及后段都已用于开发诊断；后段表现不再是独立样本外证据。'
                           + _e(required) + '</p><p class="note">本轮规则冻结记录：'
                           + _e(_time(validation.get("frozen_at_ms"))) + '（UTC+8）。冻结时间不表示这段历史此前未被查看。</p></section>')
    raw_scenarios = report.get("scenarios")
    scenarios = [item for item in raw_scenarios if isinstance(item, dict)] if isinstance(raw_scenarios, list) else []
    capital, target = _number(report.get("capital")), _number(report.get("daily_target"))
    target_pct = _pct(target / capital * 100) if capital is not None and capital > 0 and target is not None else "—"
    instruments = source.get("instruments")
    instruments_text = " / ".join(str(value) for value in instruments) if isinstance(instruments, list) else "未提供"
    ratio = _number(split.get("ratio"))
    ratio_text = _pct(ratio * 100) if ratio is not None else "未提供"
    panels = []
    assumptions = []
    for index, scenario in enumerate(scenarios):
        results = _obj(scenario.get("results"))
        for key, _ in segments:
            values = _obj(results.get(key)).get("assumptions")
            if isinstance(values, list):
                for value in values:
                    if str(value) not in assumptions:
                        assumptions.append(str(value))
        closed = _number(_obj(results.get("test")).get("closed_trades"))
        count_note = "后段平仓交易数量未提供，不能评估交易样本是否充足。" if closed is None else (
            "后段没有已平仓交易，不能据此评估策略有效性。" if closed == 0 else
            "后段平仓交易较少，观察结果不足以确认策略稳定性。" if closed < 30 else
            "交易次数不等于稳定性证明；本报告不依据后段结果自动选择策略。")
        if development:
            count_note += " 本段来自已见历史，仅用于开发诊断，不能替代独立验证。"
        panels.append('<section class="panel scenario"><div class="scenario-head"><div><span class="eyebrow">场景 ' + str(index + 1)
                      + '</span><h2>' + _e(scenario.get("label", scenario.get("id", "未命名场景")))
                      + '</h2><p class="muted">' + _e(_strategy(scenario.get("strategy"))) + ' · 单币名义入场预算上限 '
                      + _fmt(scenario.get("max_notional")) + ' USDT · 组合名义入场预算上限 ' + _fmt(scenario.get("max_total_notional"))
                      + ' USDT</p></div><span class="pill">独立共享账户模拟</span></div>' + _metrics(results, segments)
                      + '<h3>按完整北京时间自然日观察</h3>' + _daily(results, segments) + '<p class="note">' + _e(count_note) + '</p></section>')
    if not panels:
        panels.append('<section class="panel empty">没有可用研究场景，无法比较。不会用合成数据补充缺失结果。</section>')
    definition = _obj(report.get("candidate_definition"))
    definition_html = "".join('<div><dt>' + _e(key) + '</dt><dd>' + _e(json.dumps(value, ensure_ascii=False, default=str) if isinstance(value, (dict, list)) else value) + '</dd></div>' for key, value in definition.items())
    definition_heading = "本轮候选定义 · 开发复盘" if development else "固定候选规则 · 仍待验证"
    definition_text = ("本轮候选在已查看这段历史后提出，具体规则以下方定义为准。参数冻结只约束本轮计算过程，不能将重复使用的历史变成未见样本。" if development else
                       "候选仅使用整点对齐、已完成的 1H K 线：SMA 20 高于 SMA 50 且最新小时收盘高于 SMA 50 时偏多，反向条件偏空。5m 已收盘价对先前 20 根高低区间出现新的突破才发信号，持续突破不重复触发。波动距离使用 ATR 14（Wilder）× 1.5，止损比例至少 0.5%、超过 3% 的信号跳过，止盈风险收益比设为 2。")
    split_text = ("本次仅保留原时间分段作开发诊断，全部属于已见历史；后30%不再是独立样本外验证。" if development else
                  "规则在读取结果前固定，未按后段表现寻优；不能把同一个后段反复选参的结果当作独立验证。")
    curve_heading = "后段开发复盘共享账户权益轨迹" if development else "后段共享账户权益轨迹"
    group_name = {3: "三组", 4: "四组"}.get(len(scenarios), str(len(scenarios)) + "组")
    warnings = report.get("warnings")
    warnings_html = '<section class="panel notice"><h2>本次研究提示</h2><ul>' + "".join('<li>' + _e(value) + '</li>' for value in warnings) + '</ul></section>' if isinstance(warnings, list) and warnings else ''
    assumptions_context = '<p class="note">以下“固定参数”只表示本轮计算不改变参数，不表示历史此前未被查看，也不改变开发样本身份。</p>' if development else ''
    assumptions_html = '<details class="panel assumptions"><summary>运行器提供的成交与风险假设</summary>' + assumptions_context + '<ul>' + "".join('<li>' + _e(value) + '</li>' for value in assumptions) + '</ul></details>' if assumptions else ''
    return '''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; base-uri 'none'; form-action 'none'">
<title>''' + _e(title) + '''</title><style>
:root{color-scheme:dark;--bg:#0b121b;--panel:#132131;--ink:#ecf3fc;--muted:#99acc0;--line:#2a3b50;--green:#6ce2b3;--red:#ff8c99;--amber:#f4be77}*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at top right,#163a40 0,transparent 65%),var(--bg);color:var(--ink);font:14px/1.65 system-ui,"Microsoft YaHei",sans-serif}main{max-width:1480px;margin:auto;padding:36px 26px 58px}header{display:flex;justify-content:space-between;gap:25px;align-items:flex-start;margin-bottom:22px}.eyebrow{font-size:11px;letter-spacing:2px;color:var(--green);font-weight:750}h1{font-size:31px;margin:5px 0 8px;letter-spacing:-.6px}h2{font-size:19px;margin:5px 0 12px}h3{font-size:14px;margin:25px 0 11px}.muted,small{color:var(--muted)}small{display:block;font-size:11px;line-height:1.5}p{margin:8px 0}.pill{border:1px solid var(--line);color:var(--muted);border-radius:24px;padding:7px 13px;white-space:nowrap;font-size:12px}.panel{border:1px solid var(--line);background:#132131ed;border-radius:13px;padding:23px;margin:20px 0}.notice{border-color:#745b34;background:#30291f}.notice strong,.notice h2{color:var(--amber)}.cards{display:grid;grid-template-columns:repeat(3,1fr);gap:15px;margin:22px 0}.card{padding:21px;background:#142131;border:1px solid var(--line);border-radius:12px}.label{font-size:12px;color:var(--muted)}.value{font-size:30px;font-weight:680;line-height:1.5;font-variant-numeric:tabular-nums}.unit{font-size:12px;color:var(--muted);font-weight:400}.positive{color:var(--green)}.negative{color:var(--red)}.warning{color:var(--amber)}.scenario-head{display:flex;justify-content:space-between;gap:20px;align-items:center}.scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;text-align:left;font-size:12px;font-variant-numeric:tabular-nums}thead th{background:#18293b;color:#aec1d4;font-weight:550;white-space:nowrap}td,th{padding:13px 10px;border-bottom:1px solid var(--line);vertical-align:top}tbody th{min-width:130px;font-weight:600}tbody tr:last-child td,tbody tr:last-child th{border:0}.metrics-table{min-width:1200px}.metrics-table td{white-space:nowrap}.daily-table{min-width:1080px}.note{font-size:12px;color:var(--muted);margin-top:15px}.facts{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:16px 25px;margin:0}.facts dt{font-size:12px;color:var(--muted)}.facts dd{margin:4px 0 0;overflow-wrap:anywhere}.source-line{font-size:13px;color:var(--muted)}.hash{font:11px/1.6 ui-monospace,monospace;overflow-wrap:anywhere;max-width:230px}.empty{padding:35px 12px;text-align:center;color:var(--muted)}.legend{display:flex;flex-wrap:wrap;gap:10px 22px;font-size:12px;color:var(--muted);margin:8px 0 15px}.legend i{display:inline-block;width:20px;height:3px;margin-right:8px;vertical-align:middle}svg{display:block;width:100%;height:auto;max-height:350px}svg text{fill:#99acc0;font:11px system-ui,sans-serif}.gridline{stroke:#2a3b50;stroke-width:1}li{margin:7px 0}footer{color:var(--muted);font-size:12px;text-align:center;margin-top:28px}@media(max-width:800px){main{padding:24px 14px}header,.scenario-head{display:block}.pill{display:inline-block;margin-top:8px}.cards{grid-template-columns:1fr}.value{font-size:27px}.facts{grid-template-columns:1fr 1fr}.panel{padding:18px}h1{font-size:26px}}@media print{body{background:white;color:#172c39}.panel,.card{background:white;border-color:#ced9df;break-inside:avoid}.muted,small,.note,.source-line,.label,.legend,footer{color:#536978}.scroll{overflow:visible}.metrics-table,.daily-table{min-width:0;font-size:9px}.metrics-table td{white-space:normal}td,th{padding:6px 4px}thead th{background:#eaf0f4;color:#172c39}.notice{background:#fff9ee}.positive{color:#087453}.negative{color:#aa263b}}
</style></head><body><main>
<header><div><div class="eyebrow">OKX / OFFLINE PORTFOLIO RESEARCH</div><h1>''' + _e(title) + '</h1><div class="muted">' + _e(subtitle) + '</div></div><span class="pill">' + _e(badge) + '''</span></header>
''' + validation_html + '''
<section class="panel notice"><strong>每日 ''' + _fmt(target) + ''' USDT 是研究观察阈值，不是收益承诺。</strong><p>它相当于研究假设初始本金的 ''' + target_pct + ''' / 日。历史完整日达标比例仅描述本次样本，不预测未来；各场景按预设顺序展示，研究结果不自动改变运行中的交易参数。</p></section>
<div class="cards"><div class="card"><div class="label">研究假设：共享初始本金</div><div class="value">''' + _fmt(capital) + ''' <span class="unit">USDT</span></div><small>尚未确认你的实际资金。四币共用一个账户，不是每币各一份本金。</small></div><div class="card"><div class="label">每日净收益观察阈值</div><div class="value">''' + _fmt(target) + ''' <span class="unit">USDT</span></div><small>''' + target_pct + ''' 为目标换算，不是预期日收益率。</small></div><div class="card"><div class="label">本次研究场景</div><div class="value">''' + str(len(scenarios)) + ''' <span class="unit">组独立对照</span></div><small>同一历史样本，各组分别从共享虚拟本金开始，不能把''' + _e(group_name) + '''本金或收益相加。</small></div></div>
<section class="panel"><h2>''' + _e(definition_heading) + '</h2><p>' + _e(definition_text) + '</p><p class="muted">' + _e(_budget_text(report, scenarios, instruments)) + '''</p><dl class="facts">''' + definition_html + '''</dl></section>
<section class="panel"><h2>时间切分与统计口径</h2><p>按时间先后切分：前段 ''' + ratio_text + '，切分点 ' + _e(_time(split.get("train_end_ms"))) + '（UTC+8）。' + _e(split_text) + '''</p><ul><li>日统计仅包含完整北京时间自然日；不完整日单独计数，不计入历史日均、亏损日与达标比例。零交易日也计入完整日统计。</li><li>净收益来自共享账户权益变化，可能包含期末持仓按标记价格计入的未实现盈亏。期末没有强制平仓，尚未预扣未来退出手续费。</li><li>资金费盈亏正数表示收取、负数表示支付。无完整日、零平仓交易或盈利因子无定义时，不能用“—”推断盈利能力。</li><li>双倍成本场景将手续费率与滑点率同时翻倍；仍保留相同信号规则与仓位约束，不因结果不好而删去。</li></ul></section>
<section class="panel"><h2>''' + _e(curve_heading) + '</h2>' + _chart(scenarios, development) + '''<p class="note">每条线是一个独立场景的共享账户；最多抽样 400 点，仅用于显示。表格指标由完整运行结果提供，不从图形重算。曲线不作优劣排序。</p></section>
''' + warnings_html + "".join(panels) + '''
<section class="panel notice"><h2>历史成交模型仍有误差</h2><p>公开历史 OHLC、标记价格与资金费回放近似成交过程，不能还原盘口深度、订单排队、网络延迟和完整清算机制。开盘、收盘、成交及资金费采样得到的回撤也不等于真实逐笔最差路径。候选策略没有经过实盘验证，少量交易或单段达标不支持稳定日收益结论。</p></section>
''' + assumptions_html + '''
<section class="panel"><h2>公开历史缓存来源</h2><p class="source-line">合约：''' + _e(instruments_text) + ' · 周期：' + _e(source.get("bar", "未提供")) + '</p><p class="source-line">样本期间：' + _e(_time(source.get("start_ms"))) + ' 至 ' + _e(_time(source.get("end_ms"))) + '（UTC+8）；报告生成：' + _e(_time(report.get("generated_at_ms"))) + '''（UTC+8）。</p>''' + _source(source) + '''<p class="note">数据及哈希按研究文件记录展示，本页不下载或重新验证行情。研究脚本需要真实公开历史缓存及完整资金费覆盖；缺少数据应停止，而不能用合成行情替代。</p></section>
<footer>静态研究报告 · 无脚本或外部资源 · 不连接交易所、不提交订单 · 结构版本 ''' + _e(report.get("schema_version", "未提供")) + '''</footer>
</main></body></html>'''
