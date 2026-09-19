import unittest
from pathlib import Path
from unittest.mock import patch

from context_audit.picker import choose


class PickerTests(unittest.TestCase):
    def setUp(self):
        self.rows = [dict(path="/tmp/a.jsonl", client="omp", modified=0, cwd="/repo")]

    def test_latest_works_without_terminal(self):
        self.assertEqual(choose(self.rows, latest=True, interactive=False), Path("/tmp/a.jsonl"))

    def test_no_implicit_selection_without_terminal(self):
        with self.assertRaisesRegex(ValueError, "--latest"):
            choose(self.rows, interactive=False)

    @patch("builtins.print")
    @patch("builtins.input", side_effect=["99", "1"])
    def test_picker_retries_invalid_selection(self, input_mock, print_mock):
        self.assertEqual(choose(self.rows), Path("/tmp/a.jsonl"))

    @patch("builtins.print")
    @patch("builtins.input", return_value="q")
    def test_cancel(self, input_mock, print_mock):
        self.assertIsNone(choose(self.rows))
