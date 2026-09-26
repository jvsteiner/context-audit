import json
import os
import tempfile
import time
import unittest
from pathlib import Path

from context_audit.capture import CaptureStore
from context_audit.capture_report import report
from context_audit.daemon import SessionStores
from context_audit.ledger import Writer, compact, prune, records
from context_audit.recordings import requests


def payload(turns):
    return {'system': 'rules', 'tools': [{'name': 'Read'}],
            'messages': [{'role': 'user', 'content': f'turn {i}'} for i in range(turns)]}


class LedgerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_growing_session_writes_each_component_once(self):
        store = CaptureStore(self.root / 'one', 'claude')
        written = [store.request(payload(n)) for n in range(1, 40)]
        lines = [json.loads(x) for x in (self.root / 'one/events.jsonl').read_text().splitlines()]
        bodies = {json.dumps({k: v for k, v in c.items() if k not in ('position', 'pointer')}, sort_keys=True)
                  for r in written for c in r['components']}
        self.assertEqual(sum(r['type'] == 'component' for r in lines), len(bodies))
        rows = [r for r in lines if r['type'] == 'request']
        self.assertTrue(all('components' not in r for r in rows))
        self.assertEqual(len(rows[-1]['component_refs']['add']), 1)

    def test_read_back_equals_what_was_captured(self):
        store = CaptureStore(self.root / 'one', 'claude')
        written = [store.request(p) for p in (payload(2), payload(3), {'messages': ['x']}, payload(1), payload(1))]
        self.assertEqual(requests(self.root / 'one'), written)

    def test_resume_continues_chain_and_report_reads_it(self):
        CaptureStore(self.root / 'one', 'claude').request(payload(2))
        resumed = CaptureStore(self.root / 'one', 'claude')
        row = resumed.request(payload(3))
        self.assertEqual(row['delta']['retained'], 4)
        self.assertEqual(requests(self.root / 'one')[-1], row)
        self.assertEqual(len(report(self.root / 'one')['timeline']['blocks']), 5)

    def test_broken_chain_is_refused(self):
        store = CaptureStore(self.root / 'one', 'claude')
        store.request(payload(2))
        store.request(payload(3))
        path = self.root / 'one/events.jsonl'
        lines = path.read_text().splitlines(keepends=True)
        first = next(i for i, x in enumerate(lines) if json.loads(x)['type'] == 'request')
        path.write_text(''.join(lines[:first] + lines[first + 1:]))
        with self.assertRaisesRegex(ValueError, 'Broken request chain'):
            records(self.root / 'one')

    def legacy(self, directory, count):
        store = CaptureStore(directory, 'claude')
        rows = []
        for n in range(1, count + 1):
            rows.append(store.request(payload(n), session_id='s'))
        source = store.source('hook', 'Start', 'rules')
        legacy = [dict(r, schema_version=2) for r in rows]
        (directory / 'events.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in legacy[:2]) + json.dumps(source) + '\n'
                                                + ''.join(json.dumps(r) + '\n' for r in legacy[2:]))
        old = time.time() - 3600
        os.utime(directory / 'events.jsonl', (old, old))
        return legacy

    def test_compact_full_format_is_lossless(self):
        directory = self.root / 'one'
        legacy = self.legacy(directory, 30)
        before = report(directory)
        result = compact(directory)
        self.assertEqual(result['status'], 'compacted')
        self.assertLess(result['bytes_after'], result['bytes_before'] / 3)
        self.assertEqual([dict(r, schema_version=2) for r in requests(directory)], legacy)
        self.assertEqual(report(directory)['timeline'], before['timeline'])
        self.assertEqual(compact(directory)['status'], 'already-compact')
        self.assertEqual((directory / 'events.jsonl').stat().st_mode & 0o777, 0o600)

    def test_new_rows_after_full_format_rows_stay_readable(self):
        directory = self.root / 'one'
        legacy = self.legacy(directory, 3)
        row = CaptureStore(directory, 'claude').request(payload(4), session_id='s')
        self.assertEqual(requests(directory)[-1], row)
        self.assertEqual(len(requests(directory)), len(legacy) + 1)

    def test_compact_skips_recently_written(self):
        CaptureStore(self.root / 'one', 'claude').request(payload(1))
        self.assertEqual(compact(self.root / 'one')['status'], 'skipped-active')

    def test_prune_removes_oldest_idle_recordings_first(self):
        for i, name in enumerate(('old', 'middle', 'new')):
            directory = self.root / name
            directory.mkdir()
            (directory / 'events.jsonl').write_bytes(b'x' * 1000)
            stamp = time.time() - 10_000 + i * 4500
            os.utime(directory / 'events.jsonl', (stamp, stamp))
        preview = prune(self.root, 1500)
        self.assertEqual([Path(r['directory']).name for r in preview['removed']], ['old', 'middle'])
        self.assertTrue((self.root / 'old').exists())
        prune(self.root, 1500, protect=[self.root / 'old'], apply=True)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ['new', 'old'])

    def test_prune_keeps_recently_written(self):
        (self.root / 'live').mkdir()
        (self.root / 'live/events.jsonl').write_bytes(b'x' * 1000)
        self.assertEqual(prune(self.root, 10, apply=True)['removed'], [])

    def test_recorder_prunes_on_new_session_and_recovers_pruned_session(self):
        stores = SessionStores(self.root, 'claude', limit=1)
        first = stores.for_session('one')
        first.request(payload(1))
        old = time.time() - 7200
        for f in first.directory.iterdir():
            os.utime(f, (old, old))
        stores.for_session('two')
        self.assertFalse(first.directory.exists())
        again = stores.for_session('one')
        self.assertIsNot(again, first)
        self.assertEqual(again.request(payload(1))['sequence'], 1)


if __name__ == '__main__':
    unittest.main()
