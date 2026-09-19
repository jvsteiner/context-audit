import json
import re
import tempfile
import unittest
from pathlib import Path
from context_audit.capture import CaptureStore
from context_audit.capture_report import report
from context_audit.recording_view import render_recording


class SelectionTests(unittest.TestCase):
    def test_auxiliary_request_does_not_replace_default_or_reset_age(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = CaptureStore(root, 'claude')
            main = {'system': 'base', 'messages': [{'role': 'user', 'content': 'hello'}], 'tools': [{'name': 'Read'}]}
            store.request(main)
            store.request({'messages': [{'role': 'user', 'content': 'small call'}]})
            html = render_recording(root)
            pages = json.loads(re.search(r'<script id="pages" type="application/json">(.*?)</script>', html, re.S)[1])
            self.assertEqual([p['preferred'] for p in pages], [True, False, False])
            self.assertEqual(pages[0]['label'], 'Entire observed session')
            self.assertIn('no tool definitions', pages[2]['label'])
            session = report(root, session_wide=True)
            self.assertEqual(session['view_kind'], 'captured-session')
            self.assertEqual(len(session['timeline']['blocks']), 4)
            tool = next(b for b in session['timeline']['blocks'] if b['category']=='tool-definition')
            self.assertEqual(tool['details']['observed_in_requests'], [1])
            store.request(main)
            self.assertEqual({b['details']['first_seen_request'] for b in report(root)['timeline']['blocks']}, {1})
            self.assertIn('no tool definitions', report(root, 2)['coverage_notice'])

    def test_tool_free_only_uses_latest_without_hiding_requests(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = CaptureStore(root, 'claude')
            for text in ('first', 'second'):
                store.request({'messages': [{'role': 'user', 'content': text}]})
            html = render_recording(root)
            pages = json.loads(re.search(r'<script id="pages" type="application/json">(.*?)</script>', html, re.S)[1])
            self.assertEqual([p['preferred'] for p in pages], [True, False, False])
            self.assertEqual(len(report(root, session_wide=True)['timeline']['blocks']), 2)

    def test_session_preserves_old_content_and_deduplicates_repeated_snapshots(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = CaptureStore(root, 'claude')
            main = {'messages': [{'role':'user','content':'same'}, {'role':'user','content':'same'}], 'tools':[{'name':'Read'}]}
            store.request(main)
            store.request({'messages':[{'role':'user','content':'auxiliary'}]})
            store.request(main)
            store.request({'messages':[{'role':'user','content':'after compaction'}]})
            blocks = report(root, session_wide=True)['timeline']['blocks']
            self.assertEqual(len(blocks), 5)
            self.assertEqual([b['details']['first_seen_request'] for b in blocks], [1,1,1,2,4])
            self.assertEqual(blocks[0]['details']['observed_in_requests'], [1,3])
            self.assertIn('absence', blocks[0]['details']['retention'])
