import contextlib
import io
import json
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from context_audit.capture import CaptureStore
from context_audit.capture_report import report
from context_audit.daemon import SessionStores
from context_audit.gateway import Gateway
from context_audit.provenance import record_event, link_events
from context_audit.provenance_install import install, uninstall, status
from context_audit.recordings import requests
from context_audit.visualize import render


class ProvenanceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.store = CaptureStore(self.root / 'capture', 'claude')

    def event(self, **changes):
        return record_event(self.store, dict(hook_event_name='PostToolUse', tool_name='Read',
                            tool_use_id='call-1', tool_input={'file_path': '/skills/demo/SKILL.md'},
                            tool_response='PRIVATE_CONTENT_ABC', **changes))

    def request(self, content='PRIVATE_CONTENT_ABC', call='call-1'):
        return self.store.request({'messages': [{'role': 'user', 'content': [
            {'type': 'tool_result', 'tool_use_id': call, 'content': content}]}]}, session_id='session-1')

    def test_late_event_exact_link_and_privacy(self):
        self.request()
        e = self.event()
        r = report(self.store.directory, session_wide=True)
        b = r['timeline']['blocks'][0]
        self.assertEqual(b['details']['file_path'], '/skills/demo/SKILL.md')
        self.assertEqual(b['details']['provenance'][0]['evidence'], 'exact-content-and-call-id')
        self.assertEqual(r['provenance']['linked_blocks'], 1)
        self.assertEqual(r['lifecycle_events'][0]['event_id'], e['event_id'])
        self.assertNotIn('PRIVATE_CONTENT_ABC', (self.store.directory / 'events.jsonl').read_text())
        self.assertNotIn('PRIVATE_CONTENT_ABC', render(r))

    def test_transformed_result_only_call_id_evidence(self):
        self.event()
        self.request('transformed output')
        p = report(self.store.directory)['timeline']['blocks'][0]['details']['provenance'][0]
        self.assertEqual(p['evidence'], 'call-id-match')

    def test_identical_output_different_call_not_linked(self):
        self.event()
        self.request(call='unrelated')
        self.assertEqual(report(self.store.directory)['provenance']['linked_blocks'], 0)

    def test_instruction_load_metadata_not_fake_tokens(self):
        e = record_event(self.store, {'hook_event_name': 'InstructionsLoaded', 'file_path': '/CLAUDE.md',
                                     'load_reason': 'include', 'parent_file_path': '/parent/CLAUDE.md',
                                     'arbitrary_secret': 'PRIVATE_CONTENT_ABC'})
        self.request()
        r = report(self.store.directory)
        self.assertEqual(e['content_fingerprints'], [])
        self.assertEqual(r['provenance']['unmatched_events'], 1)
        self.assertEqual(r['lifecycle_events'][0]['source']['parent_file_path'], '/parent/CLAUDE.md')

    def test_post_tool_batch_documented_schema(self):
        events = record_event(self.store, {'hook_event_name': 'PostToolBatch', 'tool_calls': [
            {'tool_name': 'Read', 'tool_use_id': 'call-1', 'tool_input': {'file_path': '/a'},
             'tool_response': [{'type': 'text', 'text': 'PRIVATE_CONTENT_ABC'}]}]})
        self.request()
        self.assertEqual(events[0]['observation_point'], 'PostToolBatch')
        self.assertEqual(report(self.store.directory)['provenance']['linked_blocks'], 1)
        with self.assertRaises(ValueError):
            record_event(self.store, {'hook_event_name': 'PostToolBatch', 'tools': []})

    def test_multiple_candidates_remain_ambiguous(self):
        for tool in ('Read', 'other'):
            record_event(self.store, {'hook_event_name': 'PostToolUse', 'tool_name': tool, 'tool_response': 'same'})
        self.store.request({'messages': [{'role': 'user', 'content': 'same'}]})
        r = report(self.store.directory)
        self.assertEqual(r['provenance']['ambiguous_blocks'], 1)
        self.assertNotIn('file_path', r['timeline']['blocks'][0]['details'])

    def test_native_omp_system_content_match(self):
        s = CaptureStore(self.root / 'omp', 'omp')
        record_event(s, {'type': 'before_agent_start', 'systemPrompt': ['system instructions'], 'prompt': 'not retained'})
        s.request({'system': 'system instructions', 'messages': []})
        r = report(s.directory)
        self.assertEqual(r['provenance']['linked_blocks'], 1)
        self.assertNotIn('not retained', (s.directory / 'events.jsonl').read_text())

    def test_startup_snapshot_is_not_a_request_and_survives_restart(self):
        from context_audit.recordings import discover, resolve
        from context_audit.recording_view import render_recording
        directory = self.root / 'omp-startup'
        s = CaptureStore(directory, 'omp')
        payload = {'system': ['PRIVATE_STARTUP'], 'tools': [{'name': 'read', 'input_schema': {'type': 'object'}}]}
        s.request(payload, record_type='runtime-context', session_id='startup')
        self.assertEqual(resolve('omp', 'startup', self.root), directory)
        row = next(r for r in discover(self.root) if r['session_id'] == 'startup')
        self.assertEqual(row['requests'], 0)
        self.assertEqual(row['runtime_snapshots'], 1)
        first = report(directory, session_wide=True)
        self.assertGreater(first['timeline']['total_recorded_tokens'], 0)
        self.assertIn('0 provider requests', first['coverage_notice'])
        self.assertIn('Startup runtime snapshot', render_recording(directory))
        reopened = CaptureStore(directory, 'omp')
        sent = reopened.request(payload | {'messages': [{'role': 'user', 'content': 'hello'}]}, session_id='startup')
        self.assertEqual(sent['sequence'], 2)
        combined = report(directory, session_wide=True)
        blocks = combined['timeline']['blocks']
        self.assertEqual(len(blocks), 3)
        self.assertEqual(blocks[0]['details']['first_observation_kind'], 'runtime-context')
        self.assertEqual(blocks[-1]['category'], 'user')
        self.assertNotIn('PRIVATE_STARTUP', (directory / 'events.jsonl').read_text())

    def test_codex_native_call_matches_responses_output(self):
        s = CaptureStore(self.root / 'codex', 'codex')
        record_event(s, {'hook_event_name': 'PostToolUse', 'tool_name': 'exec_command',
                         'tool_use_id': 'call-1', 'tool_input': {'cmd': 'PRIVATE_COMMAND'},
                         'tool_response': 'PRIVATE_RESULT', 'turn_id': 'turn-1'})
        s.request({'input': [{'type': 'function_call_output', 'call_id': 'call-1', 'output': 'PRIVATE_RESULT'}]})
        links = report(s.directory)['timeline']['blocks'][0]['details']['provenance']
        self.assertEqual(links[0]['evidence'], 'exact-content-and-call-id')
        for text in ('PRIVATE_COMMAND', 'PRIVATE_RESULT'):
            self.assertNotIn(text, (s.directory / 'events.jsonl').read_text())

    def test_installer_preserves_user_hooks_and_restore(self):
        config = self.root / '.claude/settings.json'
        config.parent.mkdir()
        original = {'hooks': {'SessionStart': [{'hooks': [{'type': 'command', 'command': 'user-hook'}]}]}}
        # Fixture setup through application atomic writer.
        from context_audit.cli import atomic_write
        atomic_write(config, json.dumps(original))
        preview = install(home=self.root)
        self.assertEqual(json.loads(config.read_text()), original)
        self.assertEqual(len(preview['changes']), 3)
        install(apply=True, home=self.root)
        self.assertEqual(status(self.root)['installation'], 'installed')
        data = json.loads(config.read_text())
        data['new-user-setting'] = True
        atomic_write(config, json.dumps(data))
        uninstall(apply=True, home=self.root)
        self.assertEqual(json.loads(config.read_text()), original | {'new-user-setting': True})
        self.assertFalse((self.root / '.codex/hooks.json').exists())

    def test_changed_owned_extension_not_overwritten_on_uninstall(self):
        from context_audit.cli import atomic_write
        install(apply=True, home=self.root)
        extension = self.root / '.omp/agent/extensions/context-audit-observer.ts'
        atomic_write(extension, 'user changed this')
        with self.assertRaises(ValueError):
            uninstall(apply=True, home=self.root)
        self.assertEqual(extension.read_text(), 'user changed this')

    def test_observer_failure_is_non_decision_making(self):
        from context_audit.observer import observe_stdin
        stdin = io.TextIOWrapper(io.BytesIO(b'{"session_id":"s"}'))
        output, errors = io.StringIO(), io.StringIO()
        with patch('sys.stdin', stdin), patch('context_audit.observer.submit', side_effect=OSError('secret')):
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(errors):
                observe_stdin('claude')
        self.assertEqual(output.getvalue(), '')
        self.assertNotIn('secret', errors.getvalue())

    def test_gateway_native_ingress_session_isolation_and_validation(self):
        stores = {c: SessionStores(self.root / 'ledgers', c) for c in ('claude', 'omp')}
        gateway = Gateway('http://127.0.0.1:1', stores['claude'], event_stores=stores)
        gateway.start()
        self.addCleanup(gateway.close)
        def send(payload):
            with urllib.request.urlopen(urllib.request.Request(gateway.url + '/events', json.dumps(payload).encode())) as response:
                self.assertEqual(response.status, 204)
        for sid in ('one', 'two'):
            send({'client': 'claude', 'session_id': sid, 'payload': {'hook_event_name': 'SessionStart'}})
        send({'client': 'omp', 'session_id': 'one', 'event': 'provider_request', 'payload': {'system': 'private'}})
        send({'client': 'omp', 'session_id': 'startup', 'event': 'runtime_context', 'payload': {'system': ['private startup']}})
        startup = requests(stores['omp'].for_session('startup').directory)[0]
        self.assertEqual(startup['type'], 'runtime-context')
        self.assertEqual(startup['context_status'], 'not-a-provider-request')
        self.assertNotEqual(stores['claude'].for_session('one').key, stores['claude'].for_session('two').key)
        self.assertEqual(stores['omp'].for_session('one').sequence, 1)
        for bad in ([], {'client': 'claude', 'session_id': '../bad', 'payload': {}},
                    {'client': 'claude', 'session_id': 'one', 'payload': []}):
            with self.assertRaises(urllib.error.HTTPError) as error:
                send(bad)
            self.assertEqual(error.exception.code, 400)
