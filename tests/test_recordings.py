import json
import tempfile
import unittest
import io
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from context_audit.capture import CaptureStore
from context_audit.launch import command
from context_audit.recordings import discover, resolve, session_identity, native_session_id
from context_audit.recording_view import render_recording


class RecordingTests(unittest.TestCase):
    def test_native_lifecycle_only_session_has_explicit_pending_view(self):
        from context_audit.provenance import record_event
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            directory = root / 'omp-started'
            record_event(CaptureStore(directory, 'omp'), {'type': 'session_start'})
            self.assertEqual(resolve('omp', 'started', root), directory)
            html = render_recording(directory, 'started')
            self.assertIn('"view_kind": "captured-pending"', html)
            self.assertIn('not measured, not zero', html)
            with self.assertRaisesRegex(ValueError, 'No matching captured request'):
                render_recording(directory, 'another')
            with self.assertRaisesRegex(ValueError, 'was not captured'):
                resolve('claude', 'started', root)
            with self.assertRaisesRegex(ValueError, 'No matching captured request'):
                render_recording(directory, 'started', sequence=1)

    def test_omp_current_without_persisted_transcript(self):
        from context_audit.cli import main
        sid = '01a0b94b-6613-7357-953e-c93a8d67b2cc'
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            missing = home / f'2026-09-19T10-52-01-427Z_{sid}.jsonl'
            CaptureStore(home / '.context-audit/captures/omp-test', 'omp').request(
                {'messages': [{'role': 'user', 'content': 'test'}]}, session_id=sid)
            for identity_args in (['--session-file', str(missing)], ['--session-id', sid]):
                with patch('pathlib.Path.home', return_value=home), patch.dict('os.environ', {}, clear=True), \
                        patch('sys.argv', ['context-audit', 'current', '--client', 'omp', '--no-open', *identity_args]), \
                        redirect_stdout(io.StringIO()):
                    main()
                self.assertTrue((home / f'.context-audit/reports/omp-{sid}-capture.html').exists())
            self.assertFalse(missing.exists())

    def test_native_missing_file_never_guesses_neighboring_capture(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with self.assertRaisesRegex(ValueError, 'no native session ID'):
                native_session_id(root / 'arbitrary.jsonl')
            sid = native_session_id(root / '2026-09-19T10-52-01-427Z_01a0b94b-6613-7357-953e-c93a8d67b2cc.jsonl')
            CaptureStore(root / 'other', 'omp').request({'messages': []}, session_id='other')
            with self.assertRaisesRegex(ValueError, 'was not captured'):
                resolve('omp', sid, root)

    def test_native_existing_header_remains_authoritative(self):
        from context_audit.cli import atomic_write
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / '2026-09-19T10-52-01-427Z_01a0b94b-6613-7357-953e-c93a8d67b2cc.jsonl'
            atomic_write(path, json.dumps({'type': 'session', 'id': 'header-id'}) + '\n')
            self.assertEqual(native_session_id(path), 'header-id')
            atomic_write(path, json.dumps({'type': 'message', 'id': 'wrong'}) + '\n')
            with self.assertRaisesRegex(ValueError, 'valid native session header'):
                native_session_id(path)

    def test_native_command_uses_manager_id_not_transcript(self):
        from context_audit.integrations import integration_files
        for path, body in integration_files().items():
            if path.suffix == '.ts':
                self.assertIn('getSessionId()', body)
                self.assertIn('"--session-id", sessionId', body)
                self.assertNotIn('getSessionFile()', body)

    def test_current_cli_opens_only_exact_capture(self):
        from context_audit.cli import main
        with tempfile.TemporaryDirectory() as temp:
            home = Path(temp)
            store = CaptureStore(home / '.context-audit/captures/test', 'codex')
            store.request({'input': []}, session_id='thread-one')
            store.request({'input': []}, session_id='thread-other')
            with patch('pathlib.Path.home', return_value=home), patch('sys.argv', ['context-audit', 'current', '--client', 'codex', '--session-id', 'thread-one', '--no-open']), redirect_stdout(io.StringIO()):
                main()
            report = (home / '.context-audit/reports/codex-thread-one-capture.html').read_text()
            self.assertIn('Request 1', report)
            self.assertNotIn('Request 2', report)
            error = io.StringIO()
            with patch('pathlib.Path.home', return_value=home), patch('sys.argv', ['context-audit', 'current', '--client', 'codex', '--session-id', 'missing']), redirect_stderr(error), self.assertRaises(SystemExit) as caught:
                main()
            self.assertEqual(caught.exception.code, 2)
            self.assertIn('was not captured', error.getvalue())
            self.assertFalse((home / '.context-audit/reports/codex-missing-capture.html').exists())

    def test_exact_identity_not_latest(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = CaptureStore(root / 'one', 'codex')
            store.request({'input': []}, session_id='session-one')
            store.request({'input': []}, session_id='child-session')
            self.assertEqual(resolve('codex', 'session-one', root), root / 'one')
            with self.assertRaisesRegex(ValueError, 'was not captured'):
                resolve('codex', 'missing', root)
            with self.assertRaisesRegex(ValueError, 'was not captured'):
                resolve('claude', 'session-one', root)
            html = render_recording(root / 'one', 'session-one')
            self.assertIn('Request 1', html)
            self.assertNotIn('Request 2', html)
            self.assertEqual(len(discover(root)), 2)

    def test_session_metadata_formats(self):
        self.assertEqual(session_identity('codex', {}, {'thread-id': 'thread-1'})[0], 'thread-1')
        self.assertIsNone(session_identity('codex', {'client_metadata': {'thread_id': 'different'}}, {'thread-id': 'thread-1'})[0])
        for value in ('user_PRIVATE_account_PRIVATE_session_abc-123', json.dumps({'session_id': 'abc-123', 'account_uuid': 'PRIVATE'})):
            self.assertEqual(session_identity('claude', {'metadata': {'user_id': value}}, {})[0], 'abc-123')
        self.assertIsNone(session_identity('claude', {'metadata': {'user_id': 'arbitrary'}}, {})[0])
        self.assertIsNone(session_identity('codex', {}, {'thread-id': '../escape'})[0])

    def test_launch_preserves_environment_and_requires_auth_choice(self):
        original = {'HOME': '/home/test', 'ANTHROPIC_API_KEY': 'secret'}
        argv, env = command('claude', 'http://127.0.0.1:123/test', None, ['--model', 'test'], original)
        self.assertEqual(argv, ['claude', '--model', 'test'])
        self.assertEqual(env['HOME'], original['HOME'])
        self.assertNotIn('ANTHROPIC_BASE_URL', original)
        self.assertNotIn('secret', str(argv))
        with self.assertRaises(ValueError):
            command('codex', 'http://127.0.0.1', None, [], {})
        argv, _ = command('codex', 'http://127.0.0.1', 'chatgpt', [], {})
        self.assertIn('model_providers.context_audit.supports_websockets=false', argv)

    def test_live_partial_line_and_corruption(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / 'one'
            store = CaptureStore(directory, 'codex')
            store.request({'input': []}, session_id='one')
            with (directory / 'events.jsonl').open('a') as stream:
                stream.write('{')
            self.assertEqual(len(discover(Path(temp))), 1)
            self.assertIn('Request 1', render_recording(directory))

    def test_ambiguous_recordings_refused(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            for name in ('one', 'two'):
                CaptureStore(root / name, 'claude').request({'system': 'test'}, session_id='same')
            with self.assertRaisesRegex(ValueError, 'Multiple recordings'):
                resolve('claude', 'same', root)
