"""Self-contained, inert HTML for a historical backtest result.

This renderer does not fetch data, run strategies or infer profitability. Numeric
results and execution assumptions belong to the backtest runner's report schema.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html import escape
from typing import Any

TZ = timezone(timedelta(hours=8))
SEGMENTS = (
    ("full", "全样本"),
    ("train", "前 70%"),
    ("test", "后 30%"),
    ("test_double_costs", "后段 · 双倍成本"),
)


def _e(value: Any) -> str:
    return escape(str(value), quote=True)


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    # Guard the renderer from enormous exponents without inventing a result.
    return number if number.is_finite() and number.copy_abs() <= Decimal("1e30") else None


def _num(value: Any, places: int = 2) -> str:
    number = _decimal(value)
    return f"{number:,.{places}f}" if number is not None else "—"


def _pct(value: Any) -> str:
    number = _num(value)
    return number + "%" if number != "—" else number


def _fraction(value: Any) -> str:
    number = _decimal(value)
    return _pct(number * 100) if number is not None else "—"


def _time(value: Any) -> str:
    number = _decimal(value)
    if number is None:
        return "未提供"
    try:
        return datetime.fromtimestamp(float(number / 1000), TZ).strftime("%Y-%m-%d %H:%M")
    except (OverflowError, ValueError, OSError):
        return "时间无效"


def _period(result: dict) -> str:
    return _e(_time(result.get("start_ms"))) + "<br>至 " + _e(_time(result.get("end_ms")))


def _tone(value: Any) -> str:
    number = _decimal(value)
    return "positive" if number is not None and number > 0 else "negative" if number is not None and number < 0 else ""


def _position(result: dict) -> str:
    if "open_position" not in result:
        return "未提供"
    position = result["open_position"]
    if position is None:
        return "无"
    if not isinstance(position, dict) or not position:
        return "状态不明"
    side = {"long": "多头", "short": "空头"}.get(position.get("side"), "持仓")
    quantity = position.get("quantity", position.get("contracts"))
    detail = " · " + _num(quantity, 6) if quantity is not None else ""
    return _e(side + detail)


def _halt(result: dict) -> str:
    if result.get("halted") is True:
        return '<span class="warning">已触发</span><small>' + _e(_time(result.get("halted_at_ms"))) + "</small>"
    if result.get("halted") is False:
        return "未触发"
    return "未提供"


def _curve(result: dict, title: str) -> str:
    records = result.get("equity_curve")
    points = []
    if isinstance(records, list):
        for row in records:
            if not isinstance(row, dict):
                continue
            ts, equity = _decimal(row.get("ts")), _decimal(row.get("equity"))
            if ts is not None and equity is not None:
                points.append((ts, equity))
    if len(points) < 2:
        return '<div class="empty">可用权益点不足，未绘制曲线。</div>'
    points.sort(key=lambda row: row[0])
    # Bound HTML size. The reported drawdown is never recomputed from this chart.
    if len(points) > 600:
        last = len(points) - 1
        points = [points[index * last // 599] for index in range(600)]
    left, right, top, bottom = Decimal(76), Decimal(900), Decimal(20), Decimal(220)
    min_ts, max_ts = points[0][0], points[-1][0]
    low = min(value for _, value in points)
    high = max(value for _, value in points)
    padding = max((high - low) / 10, Decimal("0.01"))
    low, high = low - padding, high + padding
    x_span = max(Decimal(1), max_ts - min_ts)
    coordinates = " ".join(f"{left + (ts - min_ts) / x_span * (right - left):.2f},{bottom - (value - low) / (high - low) * (bottom - top):.2f}" for ts, value in points)
    grid = []
    for index in range(4):
        value = low + (high - low) * Decimal(index) / 3
        y = bottom - Decimal(index) * (bottom - top) / 3
        grid.append(f'<line x1="{left}" y1="{y:.2f}" x2="{right}" y2="{y:.2f}" class="gridline"/><text x="65" y="{y + 4:.2f}" text-anchor="end">{_e(_num(value))}</text>')
    return (
        '<svg viewBox="0 0 930 260" role="img" aria-label="' + _e(title) + '权益曲线">'
        '<title>' + _e(title) + '权益曲线，单位 USDT</title>' + "".join(grid)
        + '<polyline class="equity-line" points="' + coordinates + '"/>'
        + '<text x="76" y="248">' + _e(_time(min_ts)) + '</text>'
        + '<text x="900" y="248" text-anchor="end">' + _e(_time(max_ts)) + '</text></svg>'
    )


def _comparison(results: dict) -> str:
    rows = []
    for key, label in SEGMENTS:
        result = _dict(results.get(key))
        if not result:
            rows.append(f'<tr><th scope="row">{_e(label)}</th><td colspan="10" class="muted">无可用结果，不能据此评估策略</td></tr>')
            continue
        factor = result.get("profit_factor")
        factor_text = _num(factor) if factor is not None else "—（无定义）"
        rows.append(
            '<tr><th scope="row">' + _e(label) + '</th><td class="period">' + _period(result)
            + '</td><td class="' + _tone(result.get("net_pnl")) + '">' + _num(result.get("net_pnl"))
            + '<small>' + _pct(result.get("return_pct")) + '</small></td><td>' + _pct(result.get("max_drawdown_pct"))
            + '</td><td>' + _num(result.get("entries"), 0) + ' / ' + _num(result.get("closed_trades"), 0)
            + '</td><td>' + _pct(result.get("win_rate_pct")) + '</td><td>' + factor_text
            + '</td><td>' + _num(result.get("fees")) + '<small>资金费盈亏 ' + _num(result.get("funding_pnl"))
            + '</small></td><td>' + _num(result.get("ending_equity"))
            + '</td><td>' + _position(result) + '</td><td>' + _halt(result) + '</td></tr>'
        )
    return '<div class="table-scroll"><table class="comparison"><thead><tr><th>数据段</th><th>期间 · UTC+8</th><th>净收益 / USDT<br><small>收益率</small></th><th>最大回撤</th><th>入场 / 平仓次数</th><th>胜率</th><th>盈利因子</th><th>手续费 / USDT</th><th>期末权益 / USDT</th><th>期末未平仓</th><th>回撤停新单</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>'


def _trades(result: dict) -> str:
    trades = result.get("trades")
    rows = []
    if isinstance(trades, list):
        for trade in reversed(trades[-20:]):
            if not isinstance(trade, dict):
                continue
            side = {"long": "做多", "short": "做空"}.get(trade.get("side"), trade.get("side", "未提供"))
            reason = {"stop_loss": "止损", "take_profit": "止盈", "gap_stop_loss": "跳空止损", "gap_take_profit": "跳空止盈", "signal_reversal": "信号反转", "replay_end": "回放结束", "max_drawdown": "回撤限制"}.get(trade.get("reason"), trade.get("reason", "未提供"))
            pnl = trade.get("net_pnl", trade.get("pnl"))
            exit_time = trade.get("time", trade.get("exit_time", trade.get("exit_ms")))
            precision = {"bar": "K 线内估计", "open": "开盘估计"}.get(trade.get("exit_time_precision"), "")
            time_cell = _e(_time(exit_time)) + ('<small>' + _e(precision) + '</small>' if precision else '')
            rows.append('<tr><td>' + time_cell + '</td><td>' + _e(side)
                        + '</td><td>' + _num(trade.get("entry_price")) + '</td><td>' + _num(trade.get("exit_price"))
                        + '</td><td class="' + _tone(pnl) + '">' + _num(pnl) + '</td><td>' + _num(trade.get("fees"))
                        + '</td><td>' + _num(trade.get("funding_pnl")) + '</td><td>' + _e(reason) + '</td></tr>')
    if not rows:
        return '<div class="empty">后段没有可显示的已平仓交易。</div>'
    return '<div class="table-scroll"><table><thead><tr><th>平仓时间估计 · UTC+8</th><th>方向</th><th>开仓价</th><th>平仓价</th><th>净盈亏 / USDT</th><th>手续费 / USDT</th><th>资金费盈亏 / USDT</th><th>原因</th></tr></thead><tbody>' + "".join(rows) + '</tbody></table></div>'


def render_report(report: dict) -> str:
    """Render schema-v1 backtest metrics, with all report text HTML-escaped."""
    source, config = _dict(report.get("source")), _dict(report.get("config"))
    results, split = _dict(report.get("results")), _dict(report.get("split"))
    test, stress = _dict(results.get("test")), _dict(results.get("test_double_costs"))
    instrument = source.get("instrument", "合约未提供")
    strategy = f'{source.get("bar", "周期未提供")} · SMA {config.get("fast_window", "—")} / {config.get("slow_window", "—")}'
    heading = "真实历史 · 固定参数检验" if source.get("kind") == "production_public" else "历史回测 · 来源待确认"
    source_kind = {"production_public": "生产环境公开历史数据"}.get(source.get("kind"), "来源类型未确认：" + str(source.get("kind", "未提供")))
    config_rows = (
        ("K 线 / 均线", strategy), ("单笔目标风险", _fraction(config.get("risk_per_trade"))),
        ("各段虚拟本金", _num(test.get("initial_equity")) + " USDT"),
        ("会话回撤停止阈值", _fraction(config.get("max_drawdown_pct"))),
        ("名义价值上限", _num(config.get("max_notional")) + " USDT"),
        ("止损 / 止盈", _fraction(config.get("stop_loss_pct")) + " / " + _fraction(config.get("take_profit_pct"))),
        ("单边手续费 / 滑点假设", _fraction(config.get("fee_rate")) + " / " + _fraction(config.get("slippage_rate"))),
    )
    settings = "".join('<div><dt>' + _e(label) + '</dt><dd>' + _e(value) + '</dd></div>' for label, value in config_rows)
    warnings = report.get("warnings") if isinstance(report.get("warnings"), list) else []
    warning_html = "".join('<li>' + _e(item) + '</li>' for item in warnings)
    if warning_html:
        warning_html = '<section class="panel notice"><h2>本次数据与运行提示</h2><ul>' + warning_html + '</ul></section>'
    closed_count = _decimal(test.get("closed_trades"))
    sample_note = "后段交易数量未提供，不能评估样本是否充足。" if closed_count is None else (
        "后段没有已平仓交易，不能用胜率或盈亏比判断策略有效性。" if closed_count == 0 else
        "后段已平仓交易较少，不能据此确认策略稳定性。" if closed_count < 30 else
        "交易次数不能单独证明策略稳定性；应结合后段结果、成本敏感性及不同市场阶段观察。")
    assumptions = []
    for key, _label in SEGMENTS:
        values = _dict(results.get(key)).get("assumptions")
        if isinstance(values, list):
            for value in values:
                text = str(value)
                if text not in assumptions:
                    assumptions.append(text)
    assumptions_html = '<ul>' + "".join('<li>' + _e(item) + '</li>' for item in assumptions) + '</ul>' if assumptions else '<p class="muted">运行器未提供额外成交假设。</p>'
    return '''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; img-src 'none'; base-uri 'none'; form-action 'none'">
<title>OKX 历史回测报告 · ''' + _e(instrument) + '''</title>
<style>
:root{color-scheme:light;--ink:#172c39;--muted:#687983;--edge:#dbe4e8;--green:#126b59;--red:#b34242;--amber:#8b5b13}*{box-sizing:border-box}body{margin:0;background:#f2f6f7;color:var(--ink);font:14px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif}main{max-width:1500px;margin:auto;padding:38px 28px 54px}header{display:flex;justify-content:space-between;gap:24px;align-items:flex-start;margin-bottom:25px}.eyebrow{font-size:11px;letter-spacing:2px;color:var(--green);font-weight:750}h1{font-size:30px;letter-spacing:-1px;margin:5px 0 7px}h2{font-size:17px;margin:0 0 15px}p{margin:8px 0}.muted,small{color:var(--muted)}small{display:block;font-size:11px;line-height:1.5}.badge{display:inline-block;border:1px solid #c7d9d3;background:#edf7f3;color:var(--green);padding:6px 12px;border-radius:24px;white-space:nowrap}.panel{background:white;border:1px solid var(--edge);border-radius:13px;padding:23px;margin-bottom:20px}.notice{background:#fffaf0;border-color:#ead9b6}.notice p,.notice li{color:#735722}.metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:15px;margin:21px 0}.metric{border:1px solid var(--edge);border-radius:12px;background:white;padding:21px}.label{font-size:12px;color:var(--muted)}.value{font-size:29px;font-weight:680;line-height:1.4;font-variant-numeric:tabular-nums}.unit{font-size:12px;color:var(--muted);font-weight:400}.positive{color:var(--green)}.negative{color:var(--red)}.warning{color:var(--amber);font-weight:650}.table-scroll{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:12px;text-align:left;font-variant-numeric:tabular-nums}th,td{padding:14px 11px;border-bottom:1px solid var(--edge);vertical-align:top}thead th{background:#f5f8f9;color:#5c6e78;font-weight:600;white-space:nowrap}tbody th{min-width:108px;font-weight:650}tbody tr:last-child td,tbody tr:last-child th{border:0}.comparison{min-width:1230px}.comparison td{white-space:nowrap}.period{font-size:11px;color:var(--muted)}.facts{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:20px;margin:0}.facts div{min-width:0}.facts dt{font-size:12px;color:var(--muted);margin-bottom:5px}.facts dd{margin:0;font-weight:600;overflow-wrap:anywhere}.source{display:grid;grid-template-columns:1fr 1fr;gap:12px 30px}.source dt{color:var(--muted);font-size:12px}.source dd{margin:3px 0 0;overflow-wrap:anywhere}.hash{font:12px/1.6 ui-monospace,monospace}.empty{padding:35px 12px;color:var(--muted);text-align:center;background:#f7f9fa;border-radius:8px}svg{display:block;width:100%;height:auto;max-height:340px}svg text{fill:#687983;font:11px system-ui,sans-serif}.gridline{stroke:#e1e8ec;stroke-width:1}.equity-line{fill:none;stroke:#137866;stroke-width:2;vector-effect:non-scaling-stroke}.chart-note{font-size:11px;color:var(--muted)}ul{padding-left:22px}li{margin:6px 0}footer{color:var(--muted);font-size:12px;text-align:center;margin-top:25px}@media(max-width:850px){main{padding:25px 15px}header{display:block}.badge{margin-top:10px}.metrics{grid-template-columns:1fr 1fr}.facts{grid-template-columns:1fr 1fr}.source{grid-template-columns:1fr}h1{font-size:25px}.panel{padding:18px}.value{font-size:24px}}@media print{body{background:white}main{padding:0}.panel,.metric{break-inside:avoid;box-shadow:none}.table-scroll{overflow:visible}.comparison{min-width:0;font-size:9px}.comparison td{white-space:normal}th,td{padding:7px 4px}}
</style></head><body><main>
<header><div><div class="eyebrow">OKX · HISTORICAL BACKTEST</div><h1>''' + _e(heading) + '''</h1><p class="muted">''' + _e(instrument) + '　' + _e(strategy) + '''</p></div><span class="badge">历史回放报告 · 非交易控制台</span></header>
<section class="panel notice"><strong>这份报告不能保证未来收益。</strong><p>真实历史 OHLC 回放只近似成交过程，未模拟盘口、标记价格与成交价之间的基差或完整清算机制。结果依赖数据、成交假设与成本；少量交易不能支持稳定盈利的结论。</p></section>
<div class="metrics"><div class="metric"><div class="label">后段净收益 / USDT</div><div class="value ''' + _tone(test.get("net_pnl")) + '">' + _num(test.get("net_pnl")) + '</div><small>收益率 ' + _pct(test.get("return_pct")) + '''</small></div><div class="metric"><div class="label">后段最大回撤</div><div class="value">''' + _pct(test.get("max_drawdown_pct")) + '''</div><small>按运行器权益序列统计</small></div><div class="metric"><div class="label">后段已平仓交易</div><div class="value">''' + _num(test.get("closed_trades"), 0) + ''' <span class="unit">笔</span></div><small>''' + _e(sample_note) + '''</small></div><div class="metric"><div class="label">后段双倍成本净收益 / USDT</div><div class="value ''' + _tone(stress.get("net_pnl")) + '">' + _num(stress.get("net_pnl")) + '''</div><small>手续费率与滑点率同时翻倍</small></div></div>
<section class="panel"><h2>全样本与时间分段比较</h2><p class="muted">按时间先后切分，前段占 ''' + _fraction(split.get("ratio")) + '；切分时间 ' + _e(_time(split.get("train_end_ms"))) + '''（UTC+8）。参数固定，未按后段结果寻优。</p>''' + _comparison(results) + '''<p class="chart-note">净收益、手续费与资金费盈亏单位均为 USDT。净收益可能包含期末未平仓持仓的未实现盈亏，不代表全部已落袋。资金费盈亏正数表示收取、负数表示支付；盈利因子无定义时显示“—”，不视为零或盈利证据。期末未平仓与回撤停新单单独列示，不假定触发回撤就已平仓。</p></section>
<section class="panel"><h2>固定策略参数</h2><dl class="facts">''' + settings + '''</dl></section>
<section class="panel"><h2>后段权益曲线</h2>''' + _curve(test, "后段") + '''<p class="chart-note">图形最多抽样显示 600 个权益点，指标由完整运行结果提供，不从抽样图形重算。所有显示时间均为 UTC+8。</p></section>
''' + warning_html + '''<section class="panel"><h2>后段最近 20 笔平仓记录</h2>''' + _trades(test) + '''<p class="chart-note">只展示后段最近记录；完整成交与权益序列保留在 report.json 中。K 线内的退出时间仅按模型估计，不是真实逐笔成交时间。尚未平仓的仓位不属于此表。</p></section>
<section class="panel"><h2>数据来源与可追溯信息</h2><dl class="source"><div><dt>交易所 / 数据类型</dt><dd>''' + _e(source.get("exchange", "未提供")) + ' / ' + _e(source_kind) + '''</dd></div><div><dt>报告生成 · UTC+8</dt><dd>''' + _e(_time(report.get("generated_at_ms"))) + '''</dd></div><div><dt>历史数据期间 · UTC+8</dt><dd>''' + _e(_time(source.get("start_ms"))) + ' 至 ' + _e(_time(source.get("end_ms"))) + '''</dd></div><div><dt>数据获取 · UTC+8</dt><dd>''' + _e(_time(source.get("retrieved_at_ms"))) + '''</dd></div><div><dt>数据集 SHA-256</dt><dd class="hash">''' + _e(report.get("dataset_sha256", "未提供")) + '''</dd></div><div><dt>报告结构版本</dt><dd>''' + _e(report.get("schema_version", "未提供")) + '''</dd></div></dl></section>
<section class="panel"><h2>运行器提供的假设</h2>''' + assumptions_html + '''</section><footer>静态本地报告 · 无外部资源 · 无脚本 · 不连接交易所或提交订单</footer>
</main></body></html>'''
