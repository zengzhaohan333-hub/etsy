import copy
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html.parser import HTMLParser
import re
import unittest

from okx_bot.backtest_report import render_report


class Document(HTMLParser):
    def __init__(self, html):
        super().__init__()
        self.tags = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def example():
    run = {
        'start_ms': 0, 'end_ms': 900000, 'initial_equity': '10000',
        'ending_equity': '9985.50', 'net_pnl': '-14.50', 'return_pct': '-0.145',
        'max_drawdown_pct': '0.21', 'entries': 2, 'closed_trades': 2,
        'win_rate_pct': '50', 'profit_factor': '0.5', 'fees': '2.01',
        'funding_pnl': '-0.2', 'halted': False, 'halted_at_ms': None,
        'open_position': None, 'trades': [],
        'equity_curve': [{'ts': 0, 'equity': '10000'}, {'ts': 900000, 'equity': '9985.50'}],
        'assumptions': ['下一根 K 线开盘成交'],
    }
    return {
        'schema_version': 1, 'generated_at_ms': 0,
        'source': {'exchange': 'OKX', 'kind': 'production_public', 'instrument': 'BTC-USDT-SWAP',
                   'bar': '15m', 'start_ms': 0, 'end_ms': 900000, 'retrieved_at_ms': 0},
        'config': {'fast_window': 5, 'slow_window': 20, 'stop_loss_pct': '.01',
                   'take_profit_pct': '.02', 'risk_per_trade': '.005', 'max_notional': '200',
                   'fee_rate': '.0005', 'slippage_rate': '.0002', 'max_drawdown_pct': '.05'},
        'split': {'ratio': '.7', 'train_end_ms': 0},
        'results': {key: copy.deepcopy(run) for key in ('full', 'train', 'test', 'test_double_costs')},
        'warnings': ['历史回放近似成交'], 'dataset_sha256': 'a' * 64,
    }


class ReportTests(unittest.TestCase):
    def test_complete_report_includes_comparison_traceability_and_local_timezone(self):
        html = render_report(example())
        for value in ('<!doctype html>', '<html lang="zh-CN">', '全样本', '前 70%', '后 30%',
                      '后段 · 双倍成本', 'BTC-USDT-SWAP', '15m · SMA 5 / 20',
                      '70.00%', '1970-01-01 08:00', 'UTC+8', '-14.50',
                      '资金费盈亏', '期末未平仓', '回撤停新单', 'a' * 64):
            with self.subTest(value=value):
                self.assertIn(value, html)
        self.assertIn('<svg ', html)
        self.assertIn('未模拟盘口', html)
        self.assertIn('后段已平仓交易较少', html)
        self.assertIn('不能保证未来收益', html)

    def test_all_untrusted_text_is_escaped_and_document_has_no_active_resources(self):
        report = example()
        payload = '<script src="https://evil.invalid/a.js">alert(1)</script><img src=x onerror="bad">'
        report['source']['instrument'] = payload
        report['source']['exchange'] = payload
        report['source']['kind'] = payload
        report['config']['fast_window'] = payload
        report['warnings'] = [payload]
        report['dataset_sha256'] = payload
        report['results']['test']['assumptions'] = [payload]
        report['results']['test']['trades'] = [{'time': 0, 'side': payload, 'reason': payload}]
        html = render_report(report)
        self.assertNotIn(payload, html)
        self.assertIn('&lt;script src=&quot;', html)
        self.assertIn('&lt;img src=x onerror=&quot;bad&quot;&gt;', html)
        doc = Document(html)
        for tag, attrs in doc.tags:
            self.assertNotIn(tag, {'script', 'iframe', 'img', 'link', 'object', 'embed', 'form'})
            self.assertFalse(any(key.startswith('on') for key in attrs))
            self.assertNotIn('src', attrs)
            self.assertNotIn('href', attrs)

    def test_none_profit_factor_and_zero_trades_are_not_success_metrics(self):
        report = example()
        test = report['results']['test']
        test.update(profit_factor=None, closed_trades=0, entries=0, win_rate_pct=None,
                    equity_curve=[], net_pnl='0', return_pct='0', fees='0', funding_pnl='0')
        html = render_report(report)
        self.assertIn('—（无定义）', html)
        self.assertIn('后段没有已平仓交易', html)
        self.assertIn('可用权益点不足', html)
        self.assertIn('后段没有可显示的已平仓交易', html)
        self.assertIn('0.00%', html)

    def test_empty_report_stays_explicit_about_missing_evidence(self):
        html = render_report({})
        self.assertEqual(html.count('无可用结果，不能据此评估策略'), 4)
        self.assertIn('后段交易数量未提供', html)
        self.assertIn('来源类型未确认', html)
        self.assertNotIn('<polyline ', html)

    def test_shared_assumptions_appear_once_and_distinct_items_are_retained(self):
        report = example()
        report['results']['train']['assumptions'] = ['共享成交假设', '前段特有假设']
        report['results']['test']['assumptions'] = ['共享成交假设', '后段特有假设']
        html = render_report(report)
        for assumption in ('下一根 K 线开盘成交', '共享成交假设', '前段特有假设', '后段特有假设'):
            with self.subTest(assumption=assumption):
                self.assertEqual(html.count(assumption), 1)

    def test_initial_capital_uses_run_equity_and_drawdown_uses_config(self):
        report = example()
        report['config']['initial_equity'] = '10000'
        report['results']['test']['initial_equity'] = '5000'
        html = render_report(report)
        self.assertIn('<dt>各段虚拟本金</dt><dd>5,000.00 USDT</dd>', html)
        self.assertIn('<dt>会话回撤停止阈值</dt><dd>5.00%</dd>', html)

    def test_position_and_drawdown_halt_do_not_imply_a_closed_position(self):
        report = example()
        report['results']['test'].update(open_position={'side': 'short', 'quantity': '0.003'},
                                         halted=True, halted_at_ms=0)
        html = render_report(report)
        self.assertIn('空头 · 0.003000', html)
        self.assertIn('已触发', html)
        self.assertIn('不假定触发回撤就已平仓', html)

    def test_svg_handles_flat_series_and_bounds_large_curves(self):
        report = example()
        report['results']['test']['equity_curve'] = [{'ts': n, 'equity': '10000'} for n in range(5000)]
        html = render_report(report)
        coordinate_text = re.search(r'<polyline class="equity-line" points="([^"]+)"', html)[1]
        self.assertEqual(len(coordinate_text.split()), 600)
        self.assertNotIn('NaN', coordinate_text)
        self.assertNotIn('Infinity', coordinate_text)
        self.assertEqual(coordinate_text.split()[0].split(',')[0], '76.00')
        self.assertEqual(coordinate_text.split()[-1].split(',')[0], '900.00')

    def test_invalid_numeric_inputs_are_not_rendered_as_valid_metrics(self):
        report = example()
        report['results']['test'].update(net_pnl='NaN', max_drawdown_pct='Infinity', fees=True)
        report['results']['test']['equity_curve'] = [{'ts': 0, 'equity': 'NaN'}]
        html = render_report(report)
        self.assertNotIn('NaN', html)
        self.assertNotIn('Infinity', html)
        self.assertIn('可用权益点不足', html)

    def test_trade_table_is_limited_to_latest_twenty_and_escaped(self):
        report = example()
        report['results']['test']['trades'] = [
            {'time': index, 'side': 'long', 'entry_price': '100', 'exit_price': '101',
             'net_pnl': '1', 'fees': '.1', 'reason': f'unique-reason-{index:02d}'}
            for index in range(30)
        ]
        html = render_report(report)
        self.assertNotIn('unique-reason-09', html)
        self.assertIn('unique-reason-10', html)
        self.assertIn('unique-reason-29', html)
        self.assertEqual(len(re.findall('unique-reason-', html)), 20)

    def test_engine_trade_exit_time_precision_and_funding_are_visible(self):
        report = example()
        report['results']['test']['trades'] = [
            {'exit_time': 0, 'exit_time_precision': 'bar', 'side': 'short',
             'entry_price': '100', 'exit_price': '101', 'net_pnl': '-1.05',
             'fees': '.1', 'funding_pnl': '.05', 'reason': 'gap_stop_loss'}
        ]
        html = render_report(report)
        self.assertIn('K 线内估计', html)
        self.assertIn('跳空止损', html)
        self.assertIn('资金费盈亏 / USDT', html)
        self.assertIn('不是真实逐笔成交时间', html)

    def test_real_engine_schema_renders_closed_trade_timestamps(self):
        # Synthetic prices here are only a schema-integration fixture, not
        # historical performance evidence.
        from okx_bot.backtest_engine import run_backtest
        from okx_bot.config import Config
        from okx_bot.okx import Instrument
        from okx_bot.paper import sample_candles
        candles = sample_candles(120)
        instrument = Instrument('BTC-USDT-SWAP', Decimal('.01'), Decimal('.01'),
                                Decimal('.01'), Decimal('10000'))
        run = run_backtest(Config(), candles, candles, [], instrument,
                           starting_equity=Decimal('10000'), start_ms=candles[21].ts,
                           end_ms=candles[-1].ts + 900000)
        self.assertGreater(run['closed_trades'], 0)
        report = example()
        report['results']['test'] = run
        html = render_report(report)
        expected = datetime.fromtimestamp(run['trades'][-1]['exit_time'] / 1000,
                                          timezone(timedelta(hours=8))).strftime('%Y-%m-%d %H:%M')
        self.assertIn(expected, html)
        self.assertIn('平仓时间估计', html)


if __name__ == '__main__':
    unittest.main()
