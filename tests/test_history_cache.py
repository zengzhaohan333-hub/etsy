"""Offline page-cache integrity and atomic persistence boundaries."""
import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from okx_bot import history_cache
from okx_bot.history import HistoryError


STEP = 300_000
START = 1_767_225_600_000
END = START + 12 * STEP
INSTRUMENT = 'BTC-USDT-SWAP'


def trade_row(ts):
    return [str(ts), '100', '102', '98', '101', '0', '0', '0', '1']


def mark_row(ts):
    return [str(ts), '100', '102', '98', '101', '1']


def funding_row(ts):
    return {'instId': INSTRUMENT, 'fundingTime': str(ts),
            'fundingRate': '.01', 'realizedRate': '.0001'}


def canonical(payload):
    return json.dumps(payload, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False, allow_nan=False).encode('utf-8')


class HistoryPageCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name) / 'public cache'

    def cache(self, **changes):
        options = dict(inst_id=INSTRUMENT, bar='5m', start_ms=START,
                       end_ms=END, kind='trade')
        options.update(changes)
        return history_cache.HistoryPageCache(self.directory, **options)

    def write_payload(self, cache, payload, *, cursor=END, rehash=True):
        payload = copy.deepcopy(payload)
        if rehash:
            payload.pop('sha256', None)
            payload['sha256'] = hashlib.sha256(canonical(payload)).hexdigest()
        cache.path_for(cursor).write_bytes(canonical(payload))

    def seeded(self):
        cache = self.cache()
        rows = [trade_row(END - STEP), trade_row(END - 2 * STEP)]
        cache.save(END, rows)
        return cache, rows, json.loads(cache.path_for(END).read_text(encoding='utf-8'))

    def test_missing_page_is_a_miss_without_creating_files(self):
        cache = self.cache()
        self.assertIsNone(cache.load(END))
        self.assertFalse(self.directory.exists())

    def test_trade_mark_and_funding_pages_round_trip_without_network_or_credentials(self):
        fixtures = {'trade': [trade_row(END - STEP), trade_row(END - 2 * STEP)],
                    'mark': [mark_row(END - STEP)],
                    'funding': [funding_row(END), funding_row(START - STEP)]}
        with patch('socket.create_connection', side_effect=AssertionError('network forbidden')), \
                patch('builtins.input', side_effect=AssertionError('credentials forbidden')):
            for kind, rows in fixtures.items():
                with self.subTest(kind=kind):
                    cache = self.cache(kind=kind)
                    cursor = END + 1 if kind == 'funding' else END
                    self.assertIsNone(cache.save(cursor, rows))
                    reloaded = self.cache(kind=kind)
                    self.assertEqual(reloaded.load(cursor), rows)
                    self.assertIsInstance(cache.path_for(cursor), Path)

    def test_instrument_bar_window_kind_and_cursor_have_distinct_namespaces(self):
        caches = [self.cache(), self.cache(inst_id='ETH-USDT-SWAP'),
                  self.cache(bar='15m'), self.cache(start_ms=START + STEP),
                  self.cache(end_ms=END + STEP), self.cache(kind='mark'), self.cache(kind='funding')]
        paths = [cache.path_for(END) for cache in caches]
        self.assertEqual(len(set(paths)), len(paths))
        self.assertNotIn(caches[0].path_for(END - STEP), paths)
        caches[0].save(END, [trade_row(END - STEP)])
        self.assertTrue(all(cache.load(END) is None for cache in caches[1:]))
        self.assertIsNone(caches[0].load(END - STEP))

    def test_two_accepted_pages_survive_reconstruction_and_remain_independent(self):
        cache = self.cache()
        recent = [trade_row(END - STEP), trade_row(END - 2 * STEP)]
        older = [trade_row(END - 3 * STEP), trade_row(END - 4 * STEP)]
        cache.save(END, recent)
        cache.save(END - 2 * STEP, older)
        resumed = self.cache()
        self.assertEqual(resumed.load(END), recent)
        self.assertEqual(resumed.load(END - 2 * STEP), older)

    def test_identical_save_is_idempotent_and_conflicting_save_cannot_overwrite(self):
        cache, rows, _ = self.seeded()
        original = cache.path_for(END).read_bytes()
        with patch('okx_bot.history_cache.os.replace') as replace_file:
            cache.save(END, copy.deepcopy(rows))
            replace_file.assert_not_called()
        changed = copy.deepcopy(rows)
        changed[0][4] = '102'
        with self.assertRaises(HistoryError):
            cache.save(END, changed)
        self.assertEqual(cache.path_for(END).read_bytes(), original)
        self.assertEqual(cache.load(END), rows)

    def test_failed_atomic_replace_preserves_prior_page_and_does_not_publish_new_page(self):
        cache, rows, _ = self.seeded()
        original = cache.path_for(END).read_bytes()
        cursor = END - 2 * STEP
        with patch('okx_bot.history_cache.os.replace', side_effect=OSError('test-secret-write')):
            with self.assertRaises(HistoryError) as caught:
                cache.save(cursor, [trade_row(cursor - STEP)])
        self.assertNotIn('test-secret-write', str(caught.exception))
        self.assertEqual(cache.path_for(END).read_bytes(), original)
        self.assertEqual(self.cache().load(END), rows)
        self.assertIsNone(self.cache().load(cursor))

    def test_page_count_and_structure_rejected_before_publication(self):
        bad_rows = (None, {}, 'raw-secret', [], [trade_row(END)] * 101,
                    [None], [[]], [[{}]], [[1.25]],
                    [['x' * 513]], [{'field': 'x' * 513}], [{'field': []}])
        cache = self.cache()
        for index, rows in enumerate(bad_rows):
            with self.subTest(case=index):
                with self.assertRaises(HistoryError) as caught:
                    cache.save(END, rows)
                self.assertNotIn('raw-secret', str(caught.exception))
                self.assertFalse(cache.path_for(END).exists())

    def test_funding_rows_require_bounded_flat_objects(self):
        cache = self.cache(kind='funding')
        bad_rows = ([trade_row(END)], [{}], [{'field': []}], [{1: 'value'}],
                    [{'x' * 65: 'value'}], [{'field': 'x' * 513}],
                    [{str(index): 'value' for index in range(33)}])
        for index, rows in enumerate(bad_rows):
            with self.subTest(case=index):
                with self.assertRaises(HistoryError):
                    cache.save(END + 1, rows)
                self.assertFalse(cache.path_for(END + 1).exists())

    def test_exact_page_limit_is_accepted_and_loaded_rows_are_detached(self):
        cache = self.cache()
        rows = [trade_row(END - (index + 1) * STEP) for index in range(100)]
        cache.save(END, rows)
        rows[0][4] = '999'
        first = cache.load(END)
        self.assertEqual(len(first), 100)
        self.assertEqual(first[0][4], '101')
        first[0][4] = '888'
        self.assertEqual(cache.load(END)[0][4], '101')

    def test_invalid_cursor_is_rejected_for_every_operation(self):
        cache = self.cache()
        for cursor in (None, True, '123', 1.25, -1):
            with self.subTest(cursor_type=type(cursor).__name__):
                for operation in (cache.load, cache.path_for,
                                  lambda value: cache.save(value, [trade_row(END - STEP)])):
                    with self.assertRaises(HistoryError):
                        operation(cursor)

    def test_content_tamper_is_rejected_and_corrupt_page_is_never_overwritten(self):
        cache, rows, payload = self.seeded()
        payload['rows'][0][4] = 'test-secret-tamper'
        self.write_payload(cache, payload, rehash=False)
        corrupt = cache.path_for(END).read_bytes()
        for operation in (lambda: cache.load(END), lambda: cache.save(END, rows)):
            with self.assertRaises(HistoryError) as caught:
                operation()
            self.assertNotIn('test-secret-tamper', str(caught.exception))
            self.assertEqual(cache.path_for(END).read_bytes(), corrupt)

    def test_rehashed_schema_provenance_and_source_tampering_is_rejected(self):
        cache, _, payload = self.seeded()
        mutations = (lambda value: value.update(version=999),
                     lambda value: value.update(provenance='test-secret-private-source'),
                     lambda value: value['source'].update(host='attacker.invalid'),
                     lambda value: value['source'].update(endpoint='/api/v5/account/balance'),
                     lambda value: value.update(extra='unexpected'))
        for mutate in mutations:
            with self.subTest(mutate=mutate):
                changed = copy.deepcopy(payload)
                mutate(changed)
                self.write_payload(cache, changed)
                with self.assertRaises(HistoryError) as caught:
                    cache.load(END)
                self.assertNotIn('test-secret-private-source', str(caught.exception))

    def test_rehashed_request_must_match_all_cache_identity_fields(self):
        cache, _, payload = self.seeded()
        mutations = {'inst_id': 'ETH-USDT-SWAP', 'bar': '15m',
                     'start_ms': START + STEP, 'end_ms': END + STEP,
                     'kind': 'mark', 'cursor': END - STEP}
        for key, value in mutations.items():
            with self.subTest(field=key):
                changed = copy.deepcopy(payload)
                changed['request'][key] = value
                self.write_payload(cache, changed)
                with self.assertRaises(HistoryError):
                    cache.load(END)

    def test_malformed_json_and_nonobject_schema_fail_with_fixed_safe_error(self):
        cache, _, _ = self.seeded()
        messages = set()
        for raw in (b'{"secret":"test-secret-json"', b'[]', b'null', b'{"value":NaN}'):
            with self.subTest(raw_type=raw[:1]):
                cache.path_for(END).write_bytes(raw)
                with self.assertRaises(HistoryError) as caught:
                    cache.load(END)
                messages.add(str(caught.exception))
                self.assertNotIn('test-secret-json', str(caught.exception))
                self.assertNotIn(str(self.directory), str(caught.exception))
        self.assertEqual(len(messages), 1)

    def test_duplicate_json_fields_are_rejected_without_using_the_last_value(self):
        cache, _, payload = self.seeded()
        encoded = canonical(payload)
        cache.path_for(END).write_bytes(b'{"version":999,' + encoded[1:])
        with self.assertRaises(HistoryError):
            cache.load(END)

    def test_oversize_load_is_bounded_before_json_parse(self):
        cache, _, _ = self.seeded()
        size = cache.path_for(END).stat().st_size
        with patch('okx_bot.history_cache.MAX_PAGE_BYTES', size - 1), \
                patch('okx_bot.history_cache.json.loads') as decode:
            with self.assertRaises(HistoryError):
                cache.load(END)
            decode.assert_not_called()

    def test_oversize_save_is_rejected_without_publishing_a_page(self):
        cache = self.cache()
        with patch('okx_bot.history_cache.MAX_PAGE_BYTES', 128):
            with self.assertRaises(HistoryError):
                cache.save(END, [trade_row(END - STEP)])
        self.assertFalse(cache.path_for(END).exists())


if __name__ == '__main__':
    unittest.main()
