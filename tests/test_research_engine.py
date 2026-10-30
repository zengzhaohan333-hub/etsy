"""Shared-capital, multi-market historical execution with controlled signals."""
import json
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from decimal import Decimal as D
from types import SimpleNamespace
from unittest.mock import patch

from okx_bot import research_engine
from okx_bot.history import HistoryDataset
from okx_bot.okx import Candle, Instrument
from okx_bot.portfolio_config import PortfolioConfig


MARKETS = ('BTC-USDT-SWAP', 'ETH-USDT-SWAP', 'SOL-USDT-SWAP', 'XRP-USDT-SWAP')
STEP = 300_000
DAY = 86_400_000
EIGHT_HOURS = 8 * 3_600_000
START = int(datetime(2026, 1, 10, tzinfo=timezone.utc).timestamp() * 1000)
UNITS = dict(zip(MARKETS, map(D, ('.01', '.1', '1', '10'))))


def bar(ts, close='100', *, open_=None, low=None, high=None):
    close = D(str(close))
    opening = close if open_ is None else D(str(open_))
    lo = min(opening, close) if low is None else D(str(low))
    hi = max(opening, close) if high is None else D(str(high))
    return Candle(ts, opening, hi, lo, close)


def profile(**changes):
    defaults = dict(fast_window=2, slow_window=3, initial_equity=D(5000),
                    max_position_fraction=D(1), max_notional=D(500), max_total_notional=D(1000),
                    risk_per_trade=D('.02'), stop_loss_pct=D('.10'), take_profit_pct=D('.20'),
                    fee_rate=D(0), slippage_rate=D(0))
    defaults.update(changes)
    return PortfolioConfig(**defaults)


def history(inst_id, measured, *, marks=None, funding=None, before=None, at_end=None):
    start, end = measured[0].ts, measured[-1].ts + STEP
    warmup = [bar(start - (720 - index) * STEP) for index in range(720)]
    if funding is None:
        funding = [(ts, D(0)) for ts in range(start, end, EIGHT_HOURS)]
    instrument = Instrument(inst_id, UNITS[inst_id], D('.01'), D('.01'), D('10000000'),
                            D('.01'), inst_id.split('-')[0])
    return HistoryDataset(
        warmup + measured, warmup + (marks if marks is not None else measured), funding,
        instrument, start, end, end + STEP, bar='5m', warmup_bars=720,
        funding_before=before if before is not None else (start - STEP, D(0)),
        funding_at_end=at_end if at_end is not None else (end, D(0)))


class ResearchEngineTests(unittest.TestCase):
    def run_fixture(self, *, count=4, start=START, measured=None, marks=None, funding=None,
                    signals=None, config=None, equity=D(5000), strategy='trend_breakout_atr',
                    stop_pct=D('.10'), take_pct=D('.20'), **options):
        default = [bar(start + index * STEP) for index in range(count)]
        measured = measured or {}
        marks = marks or {}
        funding = funding or {}
        datasets = {market: history(market, measured.get(market, default),
                                     marks=marks.get(market), funding=funding.get(market)) for market in MARKETS}
        end = start + count * STEP
        signals = signals or {}
        def make_strategy(_strategy_name, config):
            class Scheduled:
                def update(self, candle):
                    side = signals.get((config.instrument, candle.ts))
                    if side is None:
                        return None
                    if not isinstance(side, str):
                        return side
                    return SimpleNamespace(side=side, stop_loss_pct=stop_pct,
                                           take_profit_pct=take_pct, atr=D(1), ready_at_ms=candle.ts + STEP)
            return Scheduled()
        with patch('okx_bot.research_engine._make_strategy', side_effect=make_strategy):
            return research_engine.run_portfolio_backtest(config or profile(), datasets,
                starting_equity=equity, start_ms=start, end_ms=end,
                strategy_name=strategy, **options)

    def run_low_turnover_fixture(self, *, count=4, signals=None, config=None,
                                 measured=None, min_lot=None, **options):
        measured = measured or {}
        datasets = {market: history(market, measured.get(market,
                    [bar(START + index * STEP) for index in range(count)])) for market in MARKETS}
        if min_lot is not None:
            datasets = {market: replace(dataset,
                        instrument=replace(dataset.instrument, min_size=min_lot))
                        for market, dataset in datasets.items()}
        signals = signals or {}
        instances = []

        class ScheduledLowTurnover:
            def __init__(self, *, fee_rate, slippage_rate):
                self.market = MARKETS[len(instances)]
                self.fee_rate = fee_rate
                self.slippage_rate = slippage_rate
                self.entries = []
                instances.append(self)

            def update(self, candle):
                side = signals.get((self.market, candle.ts))
                if side is None:
                    return None
                return SimpleNamespace(side=side, stop_loss_pct=D('.10'),
                    take_profit_pct=D('.20'), atr=D(1), ready_at_ms=candle.ts + STEP)

            def on_entry(self, ts_ms):
                self.entries.append(ts_ms)

        with patch('okx_bot.research_engine.LowTurnoverStrategy', ScheduledLowTurnover):
            result = research_engine.run_portfolio_backtest(config or profile(), datasets,
                starting_equity=D(5000), start_ms=START, end_ms=START + count * STEP,
                strategy_name='low_turnover_v2', **options)
        return result, instances

    @staticmethod
    def open_gross(result):
        return sum((D(position['quantity']) * D(position['mark_price'])
                    for position in result['open_positions'].values()), D(0))

    def test_four_assets_share_one_capital_pool_and_total_notional_cap(self):
        signals = {(market, START): 'long' for market in MARKETS}
        result = self.run_fixture(signals=signals, config=profile(max_notional=D(250)),
                                  equity=D(1000), stop_pct=D('.01'), take_pct=D('.02'))
        self.assertEqual(result['entries'], 4)
        self.assertEqual(set(result['open_positions']), set(MARKETS))
        self.assertLessEqual(self.open_gross(result), D(1000))
        self.assertEqual(D(result['initial_equity']), D(1000))
        self.assertEqual(D(result['ending_equity']), D(1000))
        for market, position in result['open_positions'].items():
            self.assertLessEqual(D(position['quantity']) * D(position['mark_price']), D(250))
            self.assertEqual(D(position['contracts']) * UNITS[market], D(position['quantity']))
            self.assertEqual(D(position['contracts']) % D('.01'), 0)
        self.assertEqual(sum(row['entries'] for row in result['per_market'].values()), 4)

    def test_mixed_long_short_positions_reserve_one_x_margin_without_fourfold_equity(self):
        sides = ('short', 'long', 'short', 'long')
        result = self.run_fixture(signals={(market, START): side for market, side in zip(MARKETS, sides)},
                                  equity=D(600), stop_pct=D('.01'), take_pct=D('.02'))
        self.assertEqual(D(result['ending_equity']), D(600))
        self.assertLessEqual(self.open_gross(result), D(600))
        self.assertEqual(result['entries'], 2)
        self.assertEqual(result['open_positions'][MARKETS[0]]['side'], 'short')
        self.assertEqual(result['open_positions'][MARKETS[1]]['side'], 'long')

    def test_total_cap_limits_simultaneous_five_hundred_per_asset_requests(self):
        result = self.run_fixture(signals={(market, START): 'long' for market in MARKETS},
                                  stop_pct=D('.01'), take_pct=D('.02'))
        self.assertEqual(result['entries'], 2)
        self.assertLessEqual(self.open_gross(result), D(1000))
        self.assertTrue(all(D(position['quantity']) * D(position['mark_price']) <= D(500)
                            for position in result['open_positions'].values()))

    def test_closed_candle_signal_enters_only_at_next_bar_open(self):
        measured = {MARKETS[0]: [bar(START), bar(START + STEP, 110)]}
        result = self.run_fixture(count=2, measured=measured,
                                  signals={(MARKETS[0], START): 'long'})
        position = result['open_positions'][MARKETS[0]]
        self.assertEqual(position['entry_time'], START + STEP)
        self.assertEqual(D(position['entry_price']), D(110))
        self.assertEqual(result['trades'], [])

    def test_signal_at_last_close_cannot_fill_past_exclusive_end(self):
        result = self.run_fixture(count=2, signals={(MARKETS[0], START + STEP): 'long'})
        self.assertEqual(result['entries'], 0)
        self.assertEqual(result['open_positions'], {})

    def test_low_turnover_entry_hook_uses_fill_time_and_ignores_warmup_and_held_signals(self):
        result, strategies = self.run_low_turnover_fixture(signals={
            (MARKETS[1], START - STEP): 'long',
            (MARKETS[0], START): 'long',
            (MARKETS[0], START + STEP): 'short',
            (MARKETS[0], START + 2 * STEP): 'long'})
        self.assertEqual(result['entries'], 1)
        self.assertEqual(strategies[0].entries, [START + STEP])
        self.assertTrue(all(strategy.entries == [] for strategy in strategies[1:]))
        self.assertEqual(result['open_positions'][MARKETS[0]]['side'], 'long')

    def test_low_turnover_shared_cap_skips_do_not_start_other_markets_cooldowns(self):
        result, strategies = self.run_low_turnover_fixture(
            signals={(market, START): 'long' for market in MARKETS},
            config=profile(max_total_notional=D(500)))
        self.assertEqual(result['entries'], 1)
        self.assertEqual(set(result['open_positions']), {MARKETS[0]})
        self.assertEqual([strategy.entries for strategy in strategies],
                         [[START + STEP], [], [], []])

    def test_low_turnover_same_bar_stop_still_notifies_the_accepted_entry_once(self):
        result, strategies = self.run_low_turnover_fixture(count=2,
            measured={MARKETS[0]: [bar(START), bar(START + STEP, low=85)]},
            signals={(MARKETS[0], START): 'long'})
        self.assertEqual(result['entries'], 1)
        self.assertEqual(result['open_positions'], {})
        self.assertEqual(len(result['trades']), 1)
        self.assertEqual(result['trades'][0]['reason'], 'stop_loss')
        self.assertEqual(strategies[0].entries, [START + STEP])
        self.assertTrue(all(strategy.entries == [] for strategy in strategies[1:]))

    def test_low_turnover_minimum_lot_skips_and_final_close_signals_have_no_entry_hook(self):
        for min_lot, signal_ts in ((D('1000000'), START), (None, START + STEP)):
            with self.subTest(min_lot=min_lot, signal_ts=signal_ts):
                result, strategies = self.run_low_turnover_fixture(count=2, min_lot=min_lot,
                    signals={(market, signal_ts): 'long' for market in MARKETS})
                self.assertEqual(result['entries'], 0)
                self.assertEqual(result['open_positions'], {})
                self.assertTrue(all(strategy.entries == [] for strategy in strategies))

    def test_low_turnover_drawdown_skip_does_not_notify_unfilled_market(self):
        measured = {MARKETS[0]: [bar(START), bar(START + STEP),
                                 bar(START + 2 * STEP, 90), bar(START + 3 * STEP, 90)]}
        result, strategies = self.run_low_turnover_fixture(measured=measured,
            config=profile(max_drawdown_pct=D('.005')),
            signals={(MARKETS[0], START): 'long', (MARKETS[1], START + STEP): 'long'})
        self.assertTrue(result['halted'])
        self.assertEqual(result['entries'], 1)
        self.assertEqual(strategies[0].entries, [START + STEP])
        self.assertTrue(all(strategy.entries == [] for strategy in strategies[1:]))

    def test_low_turnover_gate_and_execution_use_multiplied_costs_above_live_config_cap(self):
        result, strategies = self.run_low_turnover_fixture(count=2,
            config=profile(fee_rate=D('.01'), slippage_rate=D('.001')),
            signals={(MARKETS[0], START): 'long'}, fee_multiplier=D(2), slippage_multiplier=D(3))
        self.assertEqual(result['entries'], 1)
        self.assertTrue(all(strategy.fee_rate == D('.02') and strategy.slippage_rate == D('.003')
                            for strategy in strategies))
        self.assertEqual(D(result['fee_rate']), D('.02'))
        self.assertEqual(D(result['slippage_rate']), D('.003'))
        position = result['open_positions'][MARKETS[0]]
        quantity, fill = D(position['quantity']), D(position['entry_price'])
        self.assertEqual(fill, D('100.3'))
        self.assertEqual(D(result['fees']), quantity * fill * D('.02'))
        self.assertEqual(D(result['ending_equity']),
                         D(5000) - quantity * fill * D('.02') + quantity * (D(100) - fill))

    def test_existing_strategy_factories_keep_original_cost_config_under_stress(self):
        datasets = {market: history(market, [bar(START), bar(START + STEP)]) for market in MARKETS}
        config = profile(fee_rate=D('.01'), slippage_rate=D('.001'))
        for name in ('sma_cross', 'trend_breakout_atr'):
            with self.subTest(strategy=name):
                seen = []

                def make_strategy(strategy_name, market_config):
                    seen.append((strategy_name, market_config))
                    return SimpleNamespace(update=lambda candle: None)

                with patch('okx_bot.research_engine._make_strategy', side_effect=make_strategy):
                    result = research_engine.run_portfolio_backtest(config, datasets,
                        starting_equity=D(5000), start_ms=START, end_ms=START + 2 * STEP,
                        strategy_name=name, fee_multiplier=D(2), slippage_multiplier=D(3))
                self.assertEqual(len(seen), 4)
                self.assertTrue(all(strategy_name == name and market_config.fee_rate == D('.01')
                    and market_config.slippage_rate == D('.001') for strategy_name, market_config in seen))
                self.assertEqual(D(result['fee_rate']), D('.02'))
                self.assertEqual(D(result['slippage_rate']), D('.003'))

    def test_real_low_turnover_flat_history_has_no_entries(self):
        datasets = {market: history(market, [bar(START + index * STEP) for index in range(24)])
                    for market in MARKETS}
        result = research_engine.run_portfolio_backtest(profile(), datasets,
            starting_equity=D(5000), start_ms=START, end_ms=START + 24 * STEP,
            strategy_name='low_turnover_v2')
        self.assertEqual(result['entries'], 0)
        self.assertEqual(result['open_positions'], {})
        self.assertEqual(D(result['ending_equity']), D(5000))

    @staticmethod
    def sma_histories():
        measured = [bar(START, 104, open_=102), bar(START + STEP, 110)]
        datasets = {market: history(market, measured if market == MARKETS[0] else
                                    [bar(START), bar(START + STEP)]) for market in MARKETS}
        btc = datasets[MARKETS[0]]
        tail = [bar(START - (4 - index) * STEP, close)
                for index, close in enumerate((103, 102, 101, 102))]
        datasets[MARKETS[0]] = replace(btc,
            trade_candles=btc.trade_candles[:-6] + tail + measured,
            mark_candles=btc.mark_candles[:-6] + tail + measured)
        return datasets

    def test_real_sma_uses_configured_windows_and_enters_next_open_without_atr(self):
        result = research_engine.run_portfolio_backtest(profile(), self.sma_histories(),
            starting_equity=D(5000), start_ms=START, end_ms=START + 2 * STEP,
            strategy_name='sma_cross')
        self.assertEqual(result['entries'], 1)
        self.assertEqual(set(result['open_positions']), {MARKETS[0]})
        position = result['open_positions'][MARKETS[0]]
        self.assertEqual(position['side'], 'long')
        self.assertEqual(position['entry_time'], START + STEP)
        self.assertEqual(D(position['entry_price']), D(110))
        self.assertIsNone(position['atr_at_signal'])

    def test_real_sma_segment_ignores_future_and_older_than_selected_warmup_ohlc(self):
        clean = self.sma_histories()
        contaminated = {}
        for market, dataset in clean.items():
            old = Candle(START - 721 * STEP, D('NaN'), D('NaN'), D('NaN'), D('NaN'))
            future = Candle(START + 2 * STEP, D('NaN'), D('NaN'), D('NaN'), D('NaN'))
            contaminated[market] = replace(dataset,
                trade_candles=[old] + dataset.trade_candles + [future],
                mark_candles=[old] + dataset.mark_candles + [future],
                end_ms=START + 3 * STEP, warmup_bars=721)
        options = dict(starting_equity=D(5000), start_ms=START, end_ms=START + 2 * STEP,
                       strategy_name='sma_cross')
        expected = research_engine.run_portfolio_backtest(profile(), clean, **options)
        actual = research_engine.run_portfolio_backtest(profile(), contaminated, **options)
        self.assertEqual(actual, expected)
        self.assertEqual(actual['entries'], 1)

    def test_priority_rotates_per_nonempty_signal_batch_instead_of_poll_bar(self):
        signals = {(market, ts): 'long' for market in MARKETS
                   for ts in (START, START + 4 * STEP)}
        measured = {MARKETS[0]: [bar(START + index * STEP, 98 if index == 2 else 100)
                                  for index in range(6)]}
        result = self.run_fixture(count=6, measured=measured, signals=signals,
            config=profile(max_total_notional=D(500)), stop_pct=D('.01'), take_pct=D('.02'))
        self.assertEqual(result['entries'], 2)
        self.assertEqual([trade['instrument'] for trade in result['trades']], [MARKETS[0]])
        self.assertEqual(set(result['open_positions']), {MARKETS[1]})
        self.assertEqual(result['open_positions'][MARKETS[1]]['entry_time'], START + 5 * STEP)

    def test_signal_atr_and_protection_fractions_stay_frozen_at_signal_close(self):
        first = SimpleNamespace(side='long', stop_loss_pct=D('.01'), take_profit_pct=D('.02'),
                                atr=D(1), ready_at_ms=START + STEP)
        later = SimpleNamespace(side='long', stop_loss_pct=D('.03'), take_profit_pct=D('.06'),
                                atr=D(3), ready_at_ms=START + 2 * STEP)
        measured = {MARKETS[0]: [bar(START), bar(START + STEP, 110), bar(START + 2 * STEP, 110)]}
        result = self.run_fixture(count=3, measured=measured,
            signals={(MARKETS[0], START): first, (MARKETS[0], START + STEP): later})
        position = result['open_positions'][MARKETS[0]]
        self.assertEqual(result['entries'], 1)
        self.assertEqual(D(position['atr_at_signal']), D(1))
        self.assertEqual(D(position['stop_loss_pct']), D('.01'))
        self.assertEqual(D(position['take_profit_pct']), D('.02'))
        self.assertEqual(D(position['stop_loss']), D('108.9'))
        self.assertEqual(D(position['take_profit']), D('112.2'))

    def test_trade_mark_basis_cannot_exceed_cash_stop_risk_for_long_or_short(self):
        for side, trade_price in (('long', 110), ('short', 90)):
            with self.subTest(side=side):
                measured = {MARKETS[0]: [bar(START), bar(START + STEP, trade_price)]}
                marks = {MARKETS[0]: [bar(START), bar(START + STEP)]}
                result = self.run_fixture(count=2, measured=measured, marks=marks,
                    config=profile(risk_per_trade=D('.005')), stop_pct=D('.01'), take_pct=D('.02'),
                    signals={(MARKETS[0], START): side})
                position = result['open_positions'][MARKETS[0]]
                stop_distance = abs(D(position['entry_price']) - D(position['stop_loss']))
                self.assertLessEqual(D(position['quantity']) * stop_distance, D(25))
                self.assertEqual(D(position['stop_loss_pct']), D('.01'))

    def test_funding_coverage_gap_is_rejected_before_replay(self):
        datasets = self.sma_histories()
        datasets[MARKETS[0]] = replace(datasets[MARKETS[0]],
            funding_before=(START - EIGHT_HOURS - STEP, D(0)))
        with self.assertRaisesRegex(ValueError, 'Funding coverage'):
            research_engine.run_portfolio_backtest(profile(), datasets,
                starting_equity=D(5000), start_ms=START, end_ms=START + 2 * STEP,
                strategy_name='sma_cross')

    def test_all_mark_opening_gaps_exit_before_any_markets_new_entry(self):
        measured = {MARKETS[-1]: [bar(START), bar(START + STEP), bar(START + 2 * STEP, 75)]}
        marks = {MARKETS[-1]: [bar(START), bar(START + STEP), bar(START + 2 * STEP, 70)]}
        result = self.run_fixture(count=3, measured=measured, marks=marks,
            config=profile(max_total_notional=D(500)),
            signals={(MARKETS[-1], START): 'long', (MARKETS[0], START + STEP): 'long'})
        self.assertEqual(result['entries'], 2)
        self.assertEqual(set(result['open_positions']), {MARKETS[0]})
        self.assertEqual(len(result['trades']), 1)
        self.assertEqual(result['trades'][0]['instrument'], MARKETS[-1])
        self.assertEqual(result['trades'][0]['reason'], 'gap_stop_loss')
        self.assertEqual(D(result['trades'][0]['exit_price']), D(75))

    def test_two_gap_exits_release_shared_capital_before_two_new_entries_and_reconcile_funding(self):
        measured = {market: [bar(START), bar(START + STEP), bar(START + 2 * STEP, 98)]
                    for market in MARKETS[2:]}
        signals = {(market, START): 'long' for market in MARKETS[2:]}
        signals.update({(market, START + STEP): 'long' for market in MARKETS[:2]})
        result = self.run_fixture(count=3, measured=measured, signals=signals,
            stop_pct=D('.01'), take_pct=D('.02'),
            funding={MARKETS[2]: [(START + 2 * STEP, D('.01'))],
                     MARKETS[3]: [(START + 2 * STEP, D('-.01'))]})
        self.assertEqual(result['entries'], 4)
        self.assertEqual(set(result['open_positions']), set(MARKETS[:2]))
        self.assertEqual({trade['instrument'] for trade in result['trades']}, set(MARKETS[2:]))
        self.assertTrue(all(trade['reason'] == 'gap_stop_loss' for trade in result['trades']))
        self.assertEqual([D(trade['funding_pnl']) for trade in result['trades']], [D('-4.9'), D('4.9')])
        self.assertEqual(D(result['funding_pnl']), D(0))
        self.assertEqual(D(result['ending_equity']), D(4980))
        closed = sum((D(trade['net_pnl']) for trade in result['trades']), D(0))
        opened = sum((D(position['net_pnl']) for position in result['open_positions'].values()), D(0))
        self.assertEqual(D(result['ending_equity']) - D(result['initial_equity']), closed + opened)

    def test_same_bar_double_protection_uses_stop_first_for_both_directions(self):
        measured = {MARKETS[0]: [bar(START), bar(START + STEP, low=70, high=130)]}
        for side, expected in (('long', D(90)), ('short', D(110))):
            with self.subTest(side=side):
                result = self.run_fixture(count=2, measured=measured, signals={(MARKETS[0], START): side})
                self.assertEqual(len(result['trades']), 1)
                self.assertEqual(result['trades'][0]['reason'], 'stop_loss')
                self.assertEqual(D(result['trades'][0]['exit_price']), expected)

    def test_intrabar_mark_trigger_fill_is_clamped_into_trade_range(self):
        measured = {MARKETS[0]: [bar(START), bar(START + STEP),
                                 bar(START + 2 * STEP, 55, low=50, high=60)]}
        marks = {MARKETS[0]: [bar(START), bar(START + STEP),
                              bar(START + 2 * STEP, 100, low=85, high=101)]}
        result = self.run_fixture(count=3, measured=measured, marks=marks,
                                  signals={(MARKETS[0], START): 'long'})
        self.assertEqual(D(result['trades'][0]['exit_price']), D(60))
        self.assertEqual(result['trades'][0]['reason'], 'stop_loss')

    def test_trade_range_alone_cannot_trigger_a_mark_price_stop(self):
        measured = {MARKETS[0]: [bar(START), bar(START + STEP, low=50, high=150)]}
        marks = {MARKETS[0]: [bar(START), bar(START + STEP)]}
        result = self.run_fixture(count=2, measured=measured, marks=marks,
                                  signals={(MARKETS[0], START): 'long'})
        self.assertEqual(result['trades'], [])
        self.assertIn(MARKETS[0], result['open_positions'])

    def test_funding_is_settled_for_existing_holders_before_same_time_entries(self):
        sides = ('long', 'short', 'long', 'short')
        events = [(START + STEP, D('.9')), (START + 2 * STEP, D('.01'))]
        result = self.run_fixture(count=3, config=profile(max_notional=D(250)),
            funding={market: events for market in MARKETS},
            signals={(market, START): side for market, side in zip(MARKETS, sides)})
        self.assertEqual(result['entries'], 4)
        self.assertEqual(D(result['funding_pnl']), D(0))
        for market, side in zip(MARKETS, sides):
            expected = D('-2.5') if side == 'long' else D('2.5')
            self.assertEqual(D(result['per_market'][market]['funding_pnl']), expected)
            self.assertEqual(D(result['open_positions'][market]['funding_pnl']), expected)

    def test_funding_precedes_gap_exit_and_does_not_charge_new_market_entry(self):
        measured = {MARKETS[-1]: [bar(START), bar(START + STEP), bar(START + 2 * STEP, 75)]}
        marks = {MARKETS[-1]: [bar(START), bar(START + STEP), bar(START + 2 * STEP, 70)]}
        result = self.run_fixture(count=3, measured=measured, marks=marks,
            config=profile(max_total_notional=D(500)),
            funding={MARKETS[-1]: [(START + 2 * STEP, D('.01'))],
                     MARKETS[0]: [(START + 2 * STEP, D('.9'))]},
            signals={(MARKETS[-1], START): 'long', (MARKETS[0], START + STEP): 'long'})
        self.assertEqual(D(result['funding_pnl']), D('-3.5'))
        self.assertEqual(D(result['trades'][0]['funding_pnl']), D('-3.5'))
        self.assertEqual(D(result['open_positions'][MARKETS[0]]['funding_pnl']), D(0))

    def test_fee_slippage_and_closed_trade_pnl_reconcile_with_shared_equity(self):
        measured = {MARKETS[0]: [bar(START), bar(START + STEP), bar(START + 2 * STEP, high=125)]}
        config = profile(fee_rate=D('.0005'), slippage_rate=D('.001'))
        result = self.run_fixture(count=3, measured=measured, config=config,
                                  signals={(MARKETS[0], START): 'long'})
        trade = result['trades'][0]
        quantity = D(trade['quantity'])
        self.assertEqual(D(trade['entry_price']), D('100.1'))
        self.assertEqual(D(trade['exit_price']), D('119.88'))
        expected_fees = quantity * (D('100.1') + D('119.88')) * D('.0005')
        self.assertEqual(D(result['fees']), expected_fees)
        self.assertEqual(D(trade['net_pnl']), quantity * D('19.78') - expected_fees)
        self.assertEqual(D(result['ending_equity']) - D(result['initial_equity']), D(trade['net_pnl']))
        self.assertEqual(D(result['per_market'][MARKETS[0]]['fees']), expected_fees)
        json.dumps(result, allow_nan=False)

    def test_cost_stress_has_lower_net_pnl_for_the_same_profitable_signal(self):
        measured = {MARKETS[0]: [bar(START), bar(START + STEP), bar(START + 2 * STEP, high=125)]}
        options = dict(count=3, measured=measured, config=profile(fee_rate=D('.0005'), slippage_rate=D('.001')),
                       signals={(MARKETS[0], START): 'long'})
        base = self.run_fixture(**options)
        stress = self.run_fixture(**options, fee_multiplier=D(2), slippage_multiplier=D(2))
        self.assertLess(D(stress['ending_equity']), D(base['ending_equity']))

    def test_drawdown_stops_new_entries_but_keeps_holder_and_funding_active(self):
        measured = {MARKETS[0]: [bar(START), bar(START + STEP),
                                 bar(START + 2 * STEP, 94), bar(START + 3 * STEP, 94)]}
        result = self.run_fixture(config=profile(max_drawdown_pct=D('.005')), measured=measured,
            funding={MARKETS[0]: [(START + 3 * STEP, D('.01'))]},
            signals={(MARKETS[0], START): 'long', (MARKETS[1], START + 2 * STEP): 'long'})
        self.assertTrue(result['halted'])
        self.assertEqual(result['entries'], 1)
        self.assertEqual(set(result['open_positions']), {MARKETS[0]})
        self.assertEqual(result['trades'], [])
        self.assertEqual(D(result['funding_pnl']), D('-4.7'))
        self.assertEqual(D(result['ending_equity']), D('4965.3'))

    def test_daily_stats_include_zero_trade_days_and_exclude_partial_days(self):
        start = int(datetime(2026, 1, 2, 4, tzinfo=timezone.utc).timestamp() * 1000)
        end = int(datetime(2026, 1, 5, 22, tzinfo=timezone.utc).timestamp() * 1000)
        result = self.run_fixture(start=start, count=(end - start) // STEP)
        self.assertEqual([row['date'] for row in result['daily_pnl']],
                         ['2026-01-02', '2026-01-03', '2026-01-04', '2026-01-05', '2026-01-06'])
        self.assertEqual([row['complete'] for row in result['daily_pnl']], [False, True, True, True, False])
        self.assertTrue(all(D(row['net_pnl']) == 0 for row in result['daily_pnl']))
        stats = result['daily_stats']
        self.assertEqual(stats['complete_days'], 3)
        self.assertEqual(stats['partial_days'], 2)
        self.assertEqual(D(stats['mean_net_pnl']), D(0))
        self.assertEqual(D(stats['median_net_pnl']), D(0))
        self.assertEqual(stats['loss_days'], 0)
        self.assertEqual(stats['target_days'], 0)
        self.assertEqual(D(stats['target_day_ratio_pct']), D(0))

    def test_daily_mark_to_market_uses_shanghai_midnight_with_open_position(self):
        start = int(datetime(2026, 1, 2, 15, 50, tzinfo=timezone.utc).timestamp() * 1000)
        measured = {MARKETS[0]: [bar(start), bar(start + STEP, 105, open_=100),
                                 bar(start + 2 * STEP, 110, open_=105),
                                 bar(start + 3 * STEP, 100, open_=110)]}
        result = self.run_fixture(start=start, measured=measured, signals={(MARKETS[0], start): 'long'})
        self.assertEqual([row['date'] for row in result['daily_pnl']], ['2026-01-02', '2026-01-03'])
        self.assertEqual([D(row['net_pnl']) for row in result['daily_pnl']], [D(25), D(-25)])
        self.assertTrue(all(not row['complete'] for row in result['daily_pnl']))
        self.assertIn(MARKETS[0], result['open_positions'])
        stats = result['daily_stats']
        self.assertEqual(stats['complete_days'], 0)
        self.assertEqual(stats['partial_days'], 2)
        self.assertIsNone(stats['mean_net_pnl'])
        self.assertIsNone(stats['median_net_pnl'])
        self.assertIsNone(stats['target_day_ratio_pct'])

    def test_exact_day_end_does_not_create_an_empty_extra_daily_bucket(self):
        start = int(datetime(2026, 1, 1, 16, tzinfo=timezone.utc).timestamp() * 1000)
        result = self.run_fixture(start=start, count=DAY // STEP)
        self.assertEqual(len(result['daily_pnl']), 1)
        self.assertEqual(result['daily_pnl'][0]['date'], '2026-01-02')
        self.assertTrue(result['daily_pnl'][0]['complete'])
        self.assertEqual(result['daily_stats']['complete_days'], 1)
        self.assertEqual(result['daily_stats']['partial_days'], 0)


if __name__ == '__main__':
    unittest.main()
