import json
import os
import plistlib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import tomlkit

from context_audit import installation as setup
from context_audit.daemon import SessionStores


class InstallationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.project = self.home / 'project'
        self.project.mkdir()
        (self.project / 'pyproject.toml').write_text('')
        self.codex = self.home / '.codex/config.toml'
        self.claude = self.home / '.claude/settings.json'
        self.codex.parent.mkdir()
        self.claude.parent.mkdir()
        self.codex.write_text('# keep this comment\nmodel = "my-model"\n')
        self.claude.write_text('{"hooks": {"SessionStart": []}, "env": {"PRIVATE": "secret-canary"}}')
        self.before = (self.codex.read_text(), self.claude.read_text())
        self.env = patch.dict(os.environ, {}, clear=True)
        self.env.start()
        self.addCleanup(self.env.stop)

    def proposal(self):
        return setup.plan(self.home, self.project, 'chatgpt')

    def test_preview_private_and_non_mutating(self):
        p = self.proposal()
        self.assertFalse((self.home / '.context-audit').exists())
        self.assertNotIn('secret-canary', json.dumps(setup.preview(p)))
        self.assertIn('# keep this comment', p['edits'][0]['after'])
        self.assertEqual(tomlkit.parse(p['edits'][0]['after'])['model'], 'my-model')
        self.assertEqual(json.loads(p['edits'][1]['after'])['hooks'], {'SessionStart': []})
        plist = plistlib.loads(p['edits'][3]['after'].encode())
        self.assertTrue(plist['KeepAlive'])
        self.assertTrue(plist['RunAtLoad'])

    @patch('context_audit.installation.sys.platform', 'darwin')
    @patch('context_audit.installation.healthy', return_value=True)
    @patch('context_audit.installation.launchctl')
    def test_apply_restore_and_backups(self, ctl, health):
        setup.apply(self.proposal())
        self.assertEqual(tomlkit.loads(self.codex.read_text())['model_provider'], 'context_audit')
        self.assertEqual(setup.status(self.home)['state'], 'installed')
        setup.uninstall(self.home, apply_changes=True)
        self.assertEqual((self.codex.read_text(), self.claude.read_text()), self.before)
        self.assertTrue(list((self.home / '.context-audit/backups').glob('*/*')))
        self.assertFalse((self.home / '.context-audit/service.json').exists())

    @patch('context_audit.installation.sys.platform', 'darwin')
    @patch('context_audit.installation.healthy', return_value=True)
    @patch('context_audit.installation.launchctl')
    def test_changed_config_prevents_restore(self, ctl, health):
        setup.apply(self.proposal())
        self.codex.write_text(self.codex.read_text().replace('model_provider = "context_audit"', 'model_provider = "different"'))
        with self.assertRaisesRegex(ValueError, 'changed since installation'):
            setup.uninstall(self.home, True)
        self.assertIn('different', self.codex.read_text())

    @patch('context_audit.installation.sys.platform', 'darwin')
    @patch('context_audit.installation.healthy', return_value=True)
    @patch('context_audit.installation.launchctl')
    def test_unrelated_client_updates_survive_restore(self, ctl, health):
        setup.apply(self.proposal())
        self.codex.write_text(self.codex.read_text() + '\n# user change\n[projects.test]\ntrust_level = "trusted"\n')
        data = json.loads(self.claude.read_text())
        data['theme'] = 'new-theme'
        self.claude.write_text(json.dumps(data))
        self.assertTrue(setup.status(self.home)['clients']['codex']['default_routing_installed'])
        setup.uninstall(self.home, True)
        self.assertIn('# user change', self.codex.read_text())
        self.assertEqual(tomlkit.loads(self.codex.read_text())['projects']['test']['trust_level'], 'trusted')
        self.assertEqual(json.loads(self.claude.read_text())['theme'], 'new-theme')

    @patch('context_audit.installation.sys.platform', 'darwin')
    @patch('context_audit.installation.launchctl', side_effect=ValueError('service failed'))
    def test_start_failure_rolls_back_before_routing(self, ctl):
        with self.assertRaisesRegex(ValueError, 'service failed'):
            setup.apply(self.proposal())
        self.assertEqual((self.codex.read_text(), self.claude.read_text()), self.before)
        self.assertEqual(setup.status(self.home)['state'], 'uninstalled')
        self.assertFalse((self.home / '.context-audit/service.json').exists())

    def test_conflicting_routing_and_stale_preview_refused(self):
        p = self.proposal()
        self.codex.write_text('model_provider = "custom"')
        with self.assertRaisesRegex(ValueError, 'provider/endpoint'):
            self.proposal()
        with patch('context_audit.installation.sys.platform', 'darwin'), self.assertRaisesRegex(ValueError, 'changed after preview'):
            setup.apply(p)

    def test_per_session_ledgers_survive_restart(self):
        root = self.home / 'captures'
        stores = SessionStores(root, 'codex')
        stores.for_session('one').request({'input': []}, session_id='one')
        stores.for_session('two').request({'input': []}, session_id='two')
        row = SessionStores(root, 'codex').for_session('one').request({'input': []}, session_id='one')
        self.assertEqual(row['sequence'], 2)
        self.assertEqual(len(list(root.glob('*/events.jsonl'))), 2)
