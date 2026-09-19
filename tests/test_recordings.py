import json
import tempfile
import unittest
import io
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path
from unittest.mock import patch

from context_audit.capture import CaptureStore
from context_audit.launch import command
from context_audit.recordings import discover, resolve, session_identity
from context_audit.recording_view import render_recording


class RecordingTests(unittest.TestCase):
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
