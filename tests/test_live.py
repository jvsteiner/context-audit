import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from context_audit.live import current_session
from context_audit.integrations import install


class LiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)

    @patch.dict("os.environ", {}, clear=True)
    def test_exact_identity_and_no_latest_fallback(self):
        root = self.home / ".codex/sessions"
        root.mkdir(parents=True)
        chosen = root / "rollout-date-session-one.jsonl"
        chosen.write_text("{}\n")
        (root / "rollout-date-session-two.jsonl").write_text("{}\n")
        self.assertEqual(current_session("codex", "session-one", home=self.home), chosen)
        with self.assertRaisesRegex(ValueError, "found 0"):
            current_session("codex", "missing", home=self.home)

    def test_explicit_file(self):
        path = self.home / "live.jsonl"
        path.write_text("{}\n")
        self.assertEqual(current_session("omp", session_file=path), path)

    def test_installer_idempotent_and_preserves_existing_file(self):
        project = self.home / "project"
        project.mkdir()
        (project / "pyproject.toml").write_text("")
        entries = install(self.home, project)
        self.assertEqual(len(entries), 4)
        self.assertEqual(install(self.home, project), entries)
        path = Path(entries[0])
        path.write_text("user customization")
        with self.assertRaisesRegex(ValueError, "refusing to overwrite"):
            install(self.home, project)
        self.assertEqual(path.read_text(), "user customization")
