import json
import unittest

from context_audit.timeline import build_timeline
from context_audit.visualize import render
from context_audit.core import tokens


class TimelineTests(unittest.TestCase):
    def test_exclusive_skill_category_and_origin_edge(self):
        records = [(1, {"type": "message", "message": {"role": "assistant", "content": [
            {"type": "toolCall", "id": "c", "name": "read", "arguments": {"path": "skill://review"}}]}}),
            (2, {"type": "message", "message": {"role": "toolResult", "toolCallId": "c", "content": "Skill text"}})]
        report = build_timeline(records, "omp")
        call, result = report["blocks"]
        self.assertEqual(result["category"], "skill")
        self.assertEqual(result["parent"], call["id"])
        self.assertEqual(call["details"]["tool"], "read")
        self.assertEqual(call["details"]["skill"], "review")
        self.assertEqual(result["details"]["file_path"], "skill://review")
        self.assertEqual(report["by_category"]["skill"], tokens("Skill text"))
        self.assertNotIn("tool-result", report["by_category"])
        self.assertEqual(result["start_token"], call["end_token"])
        self.assertNotIn("Skill text", json.dumps(report))

    def test_compaction_keeps_history_without_claiming_retention(self):
        report = build_timeline([(1, {"type": "message", "message": {"role": "user", "content": "hello"}}),
                                 (2, {"type": "compaction", "summary": "short summary"})], "pi")
        self.assertEqual([x["category"] for x in report["blocks"]], ["user", "compaction", "summary"])
        self.assertEqual(report["blocks"][1]["details"]["retained_context"], "unknown")
        self.assertEqual(report["blocks"][2]["epoch"], 1)

    def test_linked_claude_injection_and_hook(self):
        report = build_timeline([
            (1, {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "s", "name": "Skill", "input": {"skill": "review"}}]}}),
            (2, {"type": "user", "isMeta": True, "sourceToolUseID": "s", "message": {"role": "user", "content": "Skill body"}}),
            (3, {"type": "system", "hookAdditionalContext": ["Hook body"], "hookInfos": [{"hookName": "SessionStart"}]})], "claude")
        self.assertEqual(report["blocks"][1]["category"], "skill")
        self.assertEqual(report["blocks"][2]["source"], "SessionStart")

    def test_codex_args_link_file_and_duplicate_result(self):
        call = {"type": "response_item", "payload": {"type": "function_call", "name": "read_file", "call_id": "x", "arguments": '{"path":"/repo/CLAUDE.md"}'}}
        result = {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "x", "output": "Instructions"}}
        report = build_timeline([(1, call), (2, result), (3, result)], "codex")
        self.assertEqual(len(report["blocks"]), 2)
        self.assertEqual(report["blocks"][1]["source"], "/repo/CLAUDE.md")
        self.assertEqual(report["blocks"][1]["category"], "file")
        self.assertEqual(report["blocks"][0]["details"]["file_path"], "/repo/CLAUDE.md")
        self.assertEqual(report["blocks"][1]["details"]["tool"], "read_file")

    def test_html_escapes_session_metadata(self):
        report = dict(client="claude", path="</script><script>alert(1)</script>", tokenizer="test",
                      timeline=build_timeline([], "claude"), context_snapshots=[], errors=[])
        page = render(report)
        self.assertNotIn(report["path"], page)
        self.assertIn("\\u003c/script", page)
        self.assertNotIn("https://", page)
