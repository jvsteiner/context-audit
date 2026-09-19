import json
import tempfile
import unittest
from pathlib import Path

from context_audit.capture import CaptureStore
from context_audit.capture_report import report
from context_audit.identification import identify


class IdentificationTests(unittest.TestCase):
    def test_nested_tool_catalog(self):
        value = {'role': 'developer', 'tools': [{'name': 'functions', 'tools': [
            {'name': 'exec', 'description': 'PRIVATE_SCHEMA_DESCRIPTION'}]}]}
        info = identify(value, 'tool-definition')
        self.assertEqual(info['declared_tools'], ['functions.exec'])
        self.assertEqual(info['label'], 'Tool catalog: functions')
        self.assertNotIn('PRIVATE_SCHEMA_DESCRIPTION', json.dumps(info))

    def test_nested_codex_content_and_metadata_privacy(self):
        body = ('<skills_instructions>\n## Skills\n'
                '- context-audit: PRIVATE_DESCRIPTION (file: /skills/context-audit/SKILL.md)\n'
                'PRIVATE_BODY\n## Namespace: functions\ntype exec = (_: {}) => any;')
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            store = CaptureStore(root, 'codex')
            store.request({'input': [{'role': 'developer', 'content': [{'type': 'input_text', 'text': body}]}]})
            artifact = (root / 'events.jsonl').read_text()
            self.assertNotIn('PRIVATE_BODY', artifact)
            self.assertNotIn('PRIVATE_DESCRIPTION', artifact)
            block = report(root)['timeline']['blocks'][0]
            info = block['details']['identification']
            self.assertIn('Skill catalog', block['source'])
            self.assertEqual(info['declared_skills'], [{'name': 'context-audit', 'path': '/skills/context-audit/SKILL.md'}])
            self.assertEqual(info['declared_tools'], ['exec'])
            self.assertEqual(info['identification_evidence'], 'recognized-content-markers')
            self.assertEqual(block['evidence'], 'request-structure-only')

    def test_unknown_and_parameter_values_not_exposed(self):
        self.assertEqual(identify('PRIVATE_VALUE', 'request-parameter'), {})
        self.assertEqual(identify('PRIVATE_VALUE', 'system-instructions')['label'], 'Unidentified instruction block')
        self.assertEqual(identify({'text': 'You are Claude'}, 'system-instructions')['sections'], ['Claude base instructions'])

    def test_nested_content_matches_registered_source(self):
        with tempfile.TemporaryDirectory() as temp:
            store = CaptureStore(Path(temp), 'claude')
            source = store.source('hook', 'SessionStart/example', 'hook body')
            row = store.request({'messages': [{'role': 'user', 'content': [{'type': 'text', 'text': 'hook body'}]}]})
            self.assertEqual(row['components'][0]['source_ids'], [source['source_id']])
