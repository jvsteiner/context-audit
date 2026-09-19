import json
import tempfile
import unittest
from pathlib import Path

from context_audit.capture import CaptureStore
from context_audit.capture_report import report
from context_audit.core import tokens


class ContextTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = CaptureStore(self.root, 'claude')

    def test_image_is_unknown_not_base64_text(self):
        secret = 'PRIVATE_IMAGE_BYTES' * 5000
        self.store.request({'messages': [{'role': 'user', 'content': [
            {'type': 'text', 'text': 'Hi'}, {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/png', 'data': secret}}]}]})
        t = report(self.root)['timeline']
        self.assertEqual(t['total_recorded_tokens'], tokens('Hi'))
        self.assertEqual(t['unknown_token_blocks'], 1)
        self.assertEqual(t['blocks'][1]['category'], 'media')
        self.assertNotIn(secret, (self.root / 'events.jsonl').read_text())

    def test_tool_stays_left_and_new_definitions_go_right(self):
        tool = {'name': 'Read', 'description': 'read a file'}
        first = {'role': 'user', 'content': 'hello'}
        self.store.request({'messages': [first], 'tools': [tool], 'system': 'base'})
        self.store.request({'messages': [first, {'role': 'assistant', 'content': 'reply'}, {'role': 'user', 'content': 'newest'}], 'tools': [tool], 'system': 'base'})
        bs = report(self.root)['timeline']['blocks']
        self.assertEqual([b['category'] for b in bs], ['instructions', 'tool-definition', 'user', 'assistant', 'user'])
        self.assertEqual([b['details']['first_seen_request'] for b in bs], [1, 1, 1, 2, 2])
        self.store.request({'messages': [first], 'tools': [tool, {'name': 'NewTool'}], 'system': 'base'})
        self.assertEqual(report(self.root)['timeline']['blocks'][-1]['source'], 'Tool definition: NewTool')

    def test_duplicates_removal_and_reintroduction(self):
        message = {'role': 'user', 'content': 'same'}
        self.store.request({'messages': [message]})
        self.store.request({'messages': [message, message]})
        self.assertEqual([b['details']['first_seen_request'] for b in report(self.root)['timeline']['blocks']], [1, 2])
        self.store.request({'messages': []})
        self.store.request({'messages': [message]})
        self.assertEqual(report(self.root)['timeline']['blocks'][0]['details']['first_seen_request'], 4)

    def test_nested_claude_tools_reasoning_and_reminders(self):
        self.store.request({'messages': [
            {'role': 'user', 'content': '<system-reminder>injected</system-reminder>Hi'},
            {'role': 'assistant', 'content': [{'type': 'thinking', 'thinking': 'reason', 'signature': 'SECRET_SIGNATURE'},
                                             {'type': 'tool_use', 'id': 'call', 'name': 'Read', 'input': {'path': 'file'}}]},
            {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'call', 'content': [{'type': 'text', 'text': 'result'},
                {'type': 'image', 'source': {'data': 'BINARY'}}]}]}]})
        bs = report(self.root)['timeline']['blocks']
        self.assertEqual([b['category'] for b in bs], ['injected', 'user', 'reasoning', 'tool-call', 'tool-result', 'media'])
        self.assertEqual(bs[-1]['parent'], bs[3]['id'])
        self.assertEqual(bs[-2]['details']['tool'], 'Read')
        self.assertNotIn('SECRET_SIGNATURE', (self.root / 'events.jsonl').read_text())

    def test_old_message_size_not_presented_as_text(self):
        row = self.store.request({'messages': [{'role': 'user', 'content': 'old'}], 'tools': [{'name': 'Read'}]})
        for c in row['components']:
            del c['parts']
        row['components'][0]['serialized_tokens'] = 108187
        (self.root / 'events.jsonl').write_text(json.dumps(row) + '\n')
        t = report(self.root)['timeline']
        self.assertEqual(t['unknown_token_blocks'], 1)
        self.assertLess(t['total_recorded_tokens'], 100)

    def test_codex_native_tool_call_and_result(self):
        self.store.request({'input': [{'type': 'function_call', 'call_id': 'a', 'name': 'read', 'arguments': '{}'},
                                     {'type': 'function_call_output', 'call_id': 'a', 'output': 'result'}]})
        bs = report(self.root)['timeline']['blocks']
        self.assertEqual([b['category'] for b in bs], ['tool-call', 'tool-result'])
        self.assertEqual(bs[1]['parent'], bs[0]['id'])
