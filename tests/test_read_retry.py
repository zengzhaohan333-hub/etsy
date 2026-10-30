import contextlib
import io
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, call

from okx_bot.okx import AmbiguousOrderError, OkxError
from okx_bot.read_retry import ReadRetryClient


class Stop:
    def __init__(self):
        self.stopped = False
        self.delays = []
        self.during_wait = None

    def is_set(self):
        return self.stopped

    def wait(self, delay):
        self.delays.append(delay)
        if self.during_wait:
            return self.during_wait()
        return self.stopped


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.stop = Stop()
        self.events = []
        self.recovered = Mock()
        self.credentials = object()
        self.client = SimpleNamespace(credentials=self.credentials)

    def wrapper(self, **changes):
        return ReadRetryClient(self.client, self.stop, on_retry=self.events.append,
                               on_recovered=self.recovered, **changes)

    def test_first_read_success_has_no_retry_or_recovery_callback(self):
        self.client.balances = Mock(return_value={'equity': '5000'})
        result = self.wrapper().balances()
        self.assertEqual(result, {'equity': '5000'})
        self.assertEqual(self.client.balances.call_count, 1)
        self.assertEqual(self.events, [])
        self.assertEqual(self.stop.delays, [])
        self.recovered.assert_not_called()

    def test_read_reexecutes_method_and_regenerates_auth_on_every_attempt(self):
        signatures = []
        def read_account():
            signatures.append('fresh-signature-' + str(len(signatures) + 1))
            if len(signatures) < 3:
                raise OkxError('safe timeout', category='timeout')
            return {'acctLv': '2'}
        self.client.account_config = read_account
        self.assertEqual(self.wrapper().account_config(), {'acctLv': '2'})
        self.assertEqual(signatures, ['fresh-signature-1', 'fresh-signature-2', 'fresh-signature-3'])
        self.assertEqual(self.stop.delays, [2, 4])
        self.assertEqual([e['attempt'] for e in self.events], [2, 3])
        self.recovered.assert_called_once_with()

    def test_three_total_attempts_then_reraise_last_error(self):
        errors = [OkxError('timeout-' + str(n), category='transport') for n in range(3)]
        self.client.balances = Mock(side_effect=errors)
        with self.assertRaises(OkxError) as raised:
            self.wrapper().balances()
        self.assertIs(raised.exception, errors[-1])
        self.assertEqual(self.client.balances.call_count, 3)
        self.assertEqual(self.stop.delays, [2, 4])
        self.recovered.assert_not_called()

    def test_transient_code_status_and_category_allowlist(self):
        cases = [dict(category=c) for c in ('timeout', 'dns', 'transport')]
        cases += [dict(category='http', http_status=s) for s in (408, 429, 500, 502, 503, 599)]
        cases += [dict(category='api', api_code=c) for c in ('50011', '50004', '50013')]
        for fields in cases:
            with self.subTest(fields=fields):
                error = OkxError('safe error', **fields)
                self.client.server_time = Mock(side_effect=[error, 123])
                self.assertEqual(self.wrapper().server_time(), 123)
                self.assertEqual(self.client.server_time.call_count, 2)

    def test_auth_tls_unknown_codes_and_ambiguous_errors_are_never_retried(self):
        errors = [
            OkxError('safe', category='tls', http_status=503),
            OkxError('safe', category='invalid_response', http_status=503),
            OkxError('safe', category='credential_format', api_code='50011'),
            OkxError('safe', category='transport', http_status=401, api_code='50011'),
            OkxError('safe', category='timeout', http_status=403),
            OkxError('safe', category='transport', api_code='50110', http_status=503),
            OkxError('safe', category='transport', api_code='50113'),
            OkxError('safe', category='transport', api_code='50035', http_status=503),
            OkxError('safe', category='http', http_status=400),
            OkxError('safe', category='http', http_status=407),
            OkxError('safe', category='http', http_status=302),
            AmbiguousOrderError('unknown order', category='transport', http_status=503, api_code='50004'),
            OkxError('The message says timeout, but there is no typed diagnostic'),
        ]
        for error in errors:
            with self.subTest(fields=error.__dict__):
                self.client.query_order = Mock(side_effect=error)
                with self.assertRaises(OkxError) as raised:
                    self.wrapper().query_order('BTC-USDT-SWAP', 'existing-id')
                self.assertIs(raised.exception, error)
                self.assertEqual(self.client.query_order.call_count, 1)
        self.assertEqual(self.events, [])
        self.assertEqual(self.stop.delays, [])
        self.recovered.assert_not_called()

    def test_missing_or_invalid_structured_diagnostics_fail_closed(self):
        for fields in ({'category': None}, {'api_code': 'fake-secret', 'category': 'transport'},
                       {'http_status': '503', 'category': 'transport'},
                       {'category': 'fake-secret', 'http_status': 503},
                       {'api_code': True, 'category': 'timeout'}):
            with self.subTest(fields=fields):
                error = OkxError('fake-secret')
                for name, value in fields.items():
                    setattr(error, name, value)
                self.client.positions = Mock(side_effect=error)
                with self.assertRaises(OkxError):
                    self.wrapper().positions('BTC-USDT-SWAP')
                self.assertEqual(self.client.positions.call_count, 1)
        missing = OkxError('no category')
        del missing.category
        self.client.positions = Mock(side_effect=missing)
        with self.assertRaises(OkxError):
            self.wrapper().positions('BTC-USDT-SWAP')
        self.assertEqual(self.client.positions.call_count, 1)
        self.assertEqual(self.events, [])

    def test_stop_during_backoff_reraises_original_error_without_another_read(self):
        error = OkxError('safe', category='timeout')
        self.client.candles = Mock(side_effect=[error, ['must not return stale data']])
        def interrupt():
            self.stop.stopped = True
            return True
        self.stop.during_wait = interrupt
        with self.assertRaises(OkxError) as raised:
            self.wrapper().candles('BTC-USDT-SWAP', '15m', 100)
        self.assertIs(raised.exception, error)
        self.assertEqual(self.client.candles.call_count, 1)
        self.assertEqual(self.stop.delays, [2])
        self.recovered.assert_not_called()

    def test_stop_flag_is_honored_even_if_wait_returns_false(self):
        error = OkxError('safe', category='dns')
        self.client.mark_price = Mock(side_effect=error)
        def interrupt():
            self.stop.stopped = True
            return False
        self.stop.during_wait = interrupt
        with self.assertRaises(OkxError) as raised:
            self.wrapper().mark_price('BTC-USDT-SWAP')
        self.assertIs(raised.exception, error)
        self.assertEqual(self.client.mark_price.call_count, 1)

    def test_stop_before_retry_skips_callback_and_wait(self):
        self.stop.stopped = True
        error = OkxError('safe', category='timeout')
        self.client.balances = Mock(side_effect=error)
        with self.assertRaises(OkxError) as raised:
            self.wrapper().balances()
        self.assertIs(raised.exception, error)
        self.assertEqual(self.client.balances.call_count, 1)
        self.assertEqual(self.events, [])
        self.assertEqual(self.stop.delays, [])

    def test_mutations_and_unlisted_methods_are_delegated_once(self):
        for name in ('set_leverage', 'place_market_order', 'cancel_order', 'unknown_method'):
            for fields in (dict(category='transport'), dict(api_code='50004'), dict(http_status=503)):
                with self.subTest(name=name, fields=fields):
                    error = OkxError('mutation failed', **fields)
                    method = Mock(side_effect=error)
                    setattr(self.client, name, method)
                    with self.assertRaises(OkxError) as raised:
                        getattr(self.wrapper(), name)('same-arguments', size='1')
                    self.assertIs(raised.exception, error)
                    method.assert_called_once_with('same-arguments', size='1')
        self.assertEqual(self.events, [])
        self.assertEqual(self.stop.delays, [])
        self.recovered.assert_not_called()
        self.assertIs(self.wrapper().credentials, self.credentials)

    def test_query_retry_preserves_order_identity_and_has_no_order_mutation(self):
        self.client.query_order = Mock(side_effect=[OkxError('safe', category='timeout'), {'state': 'filled'}])
        self.client.place_market_order = Mock()
        result = self.wrapper().query_order('BTC-USDT-SWAP', 'existing-client-id')
        self.assertEqual(result, {'state': 'filled'})
        self.assertEqual(self.client.query_order.call_args_list,
                         [call('BTC-USDT-SWAP', 'existing-client-id')] * 2)
        self.client.place_market_order.assert_not_called()

    def test_retry_metadata_contains_only_safe_fields_and_does_not_print(self):
        error = OkxError('fake-key fake-secret fake-passphrase', api_code='50013',
                         http_status=503, category='server')
        self.client.account_config = Mock(side_effect=[error, {'acctLv': '2'}])
        with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
            self.wrapper().account_config()
        self.assertEqual(out.getvalue() + err.getvalue(), '')
        self.assertEqual(self.events, [{'state': 'retrying', 'attempt': 2, 'max_attempts': 3,
                                       'category': 'server', 'api_code': '50013', 'http_status': 503}])
        self.assertNotIn('fake-', repr(self.events))

    def test_callbacks_order_retry_before_wait_recovered_before_return(self):
        order = []
        self.client.server_time = Mock(side_effect=[OkxError('safe', category='timeout'), 123])
        def wait(_delay):
            order.append('wait')
            return False
        self.stop.wait = wait
        wrapper = ReadRetryClient(self.client, self.stop,
            on_retry=lambda _event: order.append('retrying'), on_recovered=lambda: order.append('checking'))
        self.assertEqual(wrapper.server_time(), 123)
        order.append('returned')
        self.assertEqual(order, ['retrying', 'wait', 'checking', 'returned'])

    def test_non_okx_exceptions_are_not_retried_or_replaced(self):
        error = RuntimeError('not a typed transport error')
        self.client.balances = Mock(side_effect=error)
        with self.assertRaises(RuntimeError) as raised:
            self.wrapper().balances()
        self.assertIs(raised.exception, error)
        self.assertEqual(self.client.balances.call_count, 1)

    def test_attempt_limit_validation_and_one_attempt_mode(self):
        for limit in (0, 4, -1, True, 2.0):
            with self.subTest(limit=limit), self.assertRaises(ValueError):
                self.wrapper(max_attempts=limit)
        self.client.balances = Mock(side_effect=OkxError('safe', category='transport'))
        with self.assertRaises(OkxError):
            self.wrapper(max_attempts=1).balances()
        self.assertEqual(self.client.balances.call_count, 1)
        self.assertEqual(self.stop.delays, [])

    def test_cycle_deadline_is_shared_across_read_methods(self):
        now = [100.0]
        def account():
            now[0] += 30
            return {'acctLv': '2'}
        def positions(_instrument):
            now[0] += 31
            return []
        self.client.account_config = Mock(side_effect=account)
        self.client.positions = Mock(side_effect=positions)
        self.client.balances = Mock(return_value={'equity': '5000'})
        wrapper = self.wrapper(clock=lambda: now[0])
        wrapper.begin_cycle(60)
        self.assertEqual(wrapper.account_config(), {'acctLv': '2'})
        with self.assertRaisesRegex(OkxError, 'time budget exceeded') as raised:
            wrapper.positions('BTC-USDT-SWAP')
        self.assertEqual(raised.exception.category, 'local')
        with self.assertRaisesRegex(OkxError, 'time budget exceeded'):
            wrapper.balances()
        self.client.balances.assert_not_called()
        self.assertEqual(self.client.positions.call_count, 1)
        self.assertEqual(self.events, [])

    def test_backoff_is_limited_to_remaining_cycle_budget(self):
        now = [100.0]
        error = OkxError('safe timeout', category='timeout')
        self.client.balances = Mock(side_effect=[error, {'must_not_return': 'late data'}])
        def advance(delay):
            self.stop.delays.append(delay)
            now[0] += delay
            return False
        self.stop.wait = advance
        wrapper = self.wrapper(clock=lambda: now[0])
        wrapper.begin_cycle(1)
        with self.assertRaisesRegex(OkxError, 'time budget exceeded') as raised:
            wrapper.balances()
        self.assertEqual(raised.exception.category, 'local')
        self.assertEqual(self.stop.delays, [1])
        self.assertEqual(self.client.balances.call_count, 1)
        self.recovered.assert_not_called()

    def test_stop_at_budget_boundary_preserves_the_original_read_error(self):
        now = [0.0]
        error = OkxError('safe timeout', category='timeout')
        self.client.balances = Mock(side_effect=error)
        def interrupt(delay):
            now[0] += delay
            self.stop.stopped = True
            return True
        self.stop.wait = interrupt
        wrapper = self.wrapper(clock=lambda: now[0])
        wrapper.begin_cycle(1)
        with self.assertRaises(OkxError) as raised:
            wrapper.balances()
        self.assertIs(raised.exception, error)
        self.assertEqual(self.client.balances.call_count, 1)

    def test_slow_composite_read_cannot_return_data_after_deadline(self):
        now = [0.0]
        def pending_algos(_instrument):
            now[0] += 61
            return [{'must_not_return': 'late protective state'}]
        self.client.pending_algo_orders = Mock(side_effect=pending_algos)
        wrapper = self.wrapper(clock=lambda: now[0])
        wrapper.begin_cycle()
        with self.assertRaisesRegex(OkxError, 'time budget exceeded'):
            wrapper.pending_algo_orders('BTC-USDT-SWAP')
        self.assertEqual(self.client.pending_algo_orders.call_count, 1)
        self.assertEqual(self.events, [])

    def test_deadline_can_only_be_reset_explicitly_for_another_cycle(self):
        now = [10.0]
        self.client.server_time = Mock(return_value=123)
        wrapper = self.wrapper(clock=lambda: now[0])
        wrapper.begin_cycle(60)
        now[0] = 70
        with self.assertRaisesRegex(OkxError, 'time budget exceeded'):
            wrapper.server_time()
        self.client.server_time.assert_not_called()
        wrapper.begin_cycle(60)
        self.assertEqual(wrapper.server_time(), 123)
        self.client.server_time.assert_called_once_with()

    def test_budget_is_checked_after_failed_attempt_without_more_retries(self):
        now = [0.0]
        def fail_slowly():
            now[0] = 61
            raise OkxError('fake-secret', category='transport')
        self.client.balances = Mock(side_effect=fail_slowly)
        wrapper = self.wrapper(clock=lambda: now[0])
        wrapper.begin_cycle()
        with self.assertRaisesRegex(OkxError, 'time budget exceeded') as raised:
            wrapper.balances()
        self.assertNotIn('fake-secret', str(raised.exception))
        self.assertEqual(self.client.balances.call_count, 1)
        self.assertEqual(self.events, [])

    def test_deadline_does_not_intercept_or_retry_mutations(self):
        now = [0.0]
        self.client.set_leverage = Mock(return_value={'lever': '1'})
        wrapper = self.wrapper(clock=lambda: now[0])
        wrapper.begin_cycle(1)
        now[0] = 2
        self.assertEqual(wrapper.set_leverage('BTC-USDT-SWAP', '1'), {'lever': '1'})
        self.client.set_leverage.assert_called_once_with('BTC-USDT-SWAP', '1')

    def test_invalid_cycle_budgets_are_refused(self):
        for seconds in (0, -1, True, '60', float('nan'), float('inf')):
            with self.subTest(seconds=seconds), self.assertRaises(ValueError):
                self.wrapper().begin_cycle(seconds)


if __name__ == '__main__':
    unittest.main()
