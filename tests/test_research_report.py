import copy
from html.parser import HTMLParser
import re
import unittest

from okx_bot.research_report import render_research_report


class Parsed(HTMLParser):
    def __init__(self, text):
        super().__init__()
        self.tags = []
        self.feed(text)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, dict(attrs)))


def fixture():
    run = {'start_ms': 0, 'end_ms': 86_400_000, 'initial_equity': '5000',
           'ending_equity': '4950', 'net_pnl': '-50', 'return_pct': '-1', 'max_drawdown_pct': '2',
           'entries': 4, 'closed_trades': 3, 'fees': '5', 'funding_pnl': '-2.5', 'win_rate_pct': '33.333',
           'profit_factor': '.3', 'open_positions': {'BTC-USDT-SWAP': {'side': 'long'}},
           'halted': False, 'halted_at_ms': None,
           'equity_curve': [{'ts': 0, 'equity': '5000'}, {'ts': 86_400_000, 'equity': '4950'}],
           'daily_stats': {'complete_days': 4, 'partial_days': 1, 'mean_net_pnl': '-12.5',
                           'median_net_pnl': '-10', 'loss_days': 3, 'target_days': 1,
                           'target_day_ratio_pct': '25'}, 'daily_pnl': []}
    scenarios = []
    for strategy in ('sma_cross', 'trend_breakout_atr'):
        for cap in ('200', '500'):
            scenarios.append({'id': strategy + cap, 'label': strategy + ' / ' + cap,
                              'strategy': strategy, 'max_notional': cap, 'max_total_notional': '1000',
                              'results': {key: copy.deepcopy(run) for key in ('full', 'train', 'test', 'test_double_costs')}})
    return {'schema_version': 1, 'generated_at_ms': 0, 'mode': 'research', 'capital': '5000',
            'daily_target': '200', 'source': {'instruments': ['BTC-USDT-SWAP', 'ETH-USDT-SWAP',
                                                            'SOL-USDT-SWAP', 'XRP-USDT-SWAP'],
                                           'bar': '5m', 'start_ms': 0, 'end_ms': 86_400_000,
                                           'datasets': {'BTC-USDT-SWAP': {'filename': 'btc-history.json',
                                                                         'retrieved_at_ms': 0, 'sha256': 'a' * 64}}},
            'split': {'ratio': '.7', 'train_end_ms': 0}, 'candidate_definition': {'趋势周期': '1H', 'ATR': '14 Wilder'},
            'scenarios': scenarios, 'warnings': ['完整资金费样本，成交仅为模型近似。']}


class ResearchReportTests(unittest.TestCase):
    def test_shared_capital_target_and_all_scenarios_are_observations_not_promises(self):
        html = render_research_report(fixture())
        for text in ('研究假设：共享初始本金', '5,000.00', '4.00%', '尚未确认你的实际资金',
                     '不是收益承诺', '非预测', '不能把四组本金或收益相加', '候选策略未验证',
                     '1,000.00 USDT', '4 × 200 = 800', '1970-01-01 08:00', 'UTC+8', 'a' * 64):
            with self.subTest(text=text):
                self.assertIn(text, html)
        self.assertEqual(html.count('class="panel scenario"'), 4)
        self.assertEqual(html.count('class="metrics-table"'), 4)
        self.assertEqual(html.count('class="daily-table"'), 4)
        self.assertIn('-50.00', html)
        self.assertIn('-2.50', html)
        self.assertIn('25.00%', html)
        self.assertIn('class="negative"', html)
        self.assertNotIn('20,000.00', html)

    def test_custom_entry_budget_cap_is_not_replaced_by_a_hardcoded_total(self):
        report = fixture()
        report['profile'] = {'max_total_notional': '750'}
        for scenario in report['scenarios']:
            scenario['max_total_notional'] = '750'
        html = render_research_report(report)
        self.assertIn('组合名义入场预算上限共同为 750.00 USDT', html)
        self.assertIn('合计入场预算至多 750 USDT', html)
        self.assertIn('4 × 200 = 800', html)
        self.assertIn('标记价格变化可能使名义敞口超过预算', html)
        self.assertIn('系统不会因此自动减仓', html)
        self.assertNotIn('组合名义硬上限', html)
        self.assertNotIn('1,000', html)
        for scenario in report['scenarios']:
            scenario.pop('max_total_notional')
        fallback = render_research_report(report)
        self.assertIn('研究配置中的组合名义入场预算上限为 750.00 USDT', fallback)
        self.assertNotIn('1,000', fallback)

    def test_comparison_keeps_every_segment_costs_open_positions_and_daily_denominators(self):
        html = render_research_report(fixture())
        for text in ('全样本', '前 70%', '后 30%', '后段 · 双倍手续费与滑点', '手续费 / USDT',
                     '资金费盈亏 / USDT', '1 个未平仓', '完整日数', '不完整日数', '亏损日数',
                     '历史完整日达标比例', '零交易日也计入', '未预扣未来退出手续费'):
            with self.subTest(text=text):
                self.assertIn(text, html)

    def test_no_complete_days_are_not_a_zero_success_rate(self):
        report = fixture()
        run = report['scenarios'][0]['results']['test']
        run.update(closed_trades=0, entries=0, win_rate_pct=None, profit_factor=None,
                   open_positions={}, equity_curve=[])
        run['daily_stats'].update(complete_days=0, partial_days=2, mean_net_pnl=None,
                                  median_net_pnl=None, target_days=0, target_day_ratio_pct=None)
        html = render_research_report(report)
        self.assertIn('—（无完整日）', html)
        self.assertIn('—（无定义）', html)
        self.assertIn('后段没有已平仓交易', html)

    def test_empty_report_never_synthesizes_missing_results(self):
        html = render_research_report({})
        self.assertIn('没有可用研究场景，无法比较', html)
        self.assertIn('不会用合成数据补充', html)
        self.assertIn('数据来源元信息未提供', html)
        self.assertNotIn('class="panel scenario"', html)

    def test_every_external_text_field_is_escaped_and_page_is_passive(self):
        report = fixture()
        payload = '<script src="https://evil.invalid/a.js">bad()</script><img src=x onerror="bad()">'
        report['source']['instruments'] = [payload]
        report['source']['bar'] = payload
        report['source']['datasets'] = {payload: {'filename': payload, 'sha256': payload}}
        report['candidate_definition'] = {payload: {'nested': payload}}
        report['warnings'] = [payload]
        report['scenarios'][0].update(id=payload, label=payload, strategy=payload)
        report['scenarios'][0]['results']['test']['open_positions'] = {payload: {'side': payload}}
        for scenario in report['scenarios']:
            scenario['results']['test']['assumptions'] = [payload]
        html = render_research_report(report)
        self.assertNotIn(payload, html)
        self.assertIn('&lt;script src=&quot;', html)
        self.assertEqual(html.count('<li>&lt;script'), 2)  # warnings + deduplicated assumptions
        for tag, attrs in Parsed(html).tags:
            self.assertNotIn(tag, {'script', 'iframe', 'object', 'embed', 'img', 'link', 'form'})
            self.assertNotIn('src', attrs)
            self.assertNotIn('href', attrs)
            self.assertFalse(any(key.startswith('on') for key in attrs))

    def test_svg_is_bounded_and_does_not_rank_scenarios(self):
        report = fixture()
        for scenario in report['scenarios']:
            scenario['results']['test']['equity_curve'] = [{'ts': i, 'equity': '5000'} for i in range(5000)]
        html = render_research_report(report)
        curves = re.findall(r'<polyline[^>]*points="([^"]+)"', html)
        self.assertEqual(len(curves), 4)
        self.assertTrue(all(len(curve.split()) == 400 for curve in curves))
        self.assertTrue(all('NaN' not in curve and 'Infinity' not in curve for curve in curves))
        labels = [scenario['label'] for scenario in report['scenarios']]
        self.assertLess(html.index(labels[0]), html.index(labels[-1]))
        self.assertIn('曲线不作优劣排序', html)

    def test_non_finite_metrics_and_undefined_capital_are_not_valid_returns(self):
        report = fixture()
        report['capital'] = '0'
        report['scenarios'][0]['results']['test'].update(net_pnl='NaN', max_drawdown_pct='Infinity')
        html = render_research_report(report)
        self.assertNotIn('NaN', html)
        self.assertNotIn('Infinity', html)
        self.assertIn('— / 日', html)


if __name__ == '__main__':
    unittest.main()
