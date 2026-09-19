import json
import tempfile
import unittest
from pathlib import Path

from context_audit.sessions import analyze, discover
from context_audit.core import tokens


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def session(self, events, name="session.jsonl"):
        path = self.root / name
        path.write_text("\n".join(json.dumps(x) for x in events))
        return path

    def test_claude_skill_attribution_counts_historical_output_not_disk(self):
        events = [
            {"type": "assistant", "message": {"role": "assistant", "id": "a", "usage": {"input_tokens": 10},
                "content": [{"type": "tool_use", "id": "r", "name": "Read", "input": {"file_path": "/missing/skills/review/SKILL.md"}}]}},
            {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "r", "content": "Review security boundaries."}]}},
            {"type": "assistant", "message": {"role": "assistant", "id": "a", "usage": {"input_tokens": 12}, "content": []}},
        ]
        report = analyze(self.session(events))
        self.assertEqual(report["attributed_skill_returned_tokens"], tokens("Review security boundaries."))
        self.assertEqual(report["request_count"], 1)
        self.assertEqual(report["reported_request_usage"]["input_tokens"], 12)

    def test_pi_and_omp_native_tools_and_context_snapshots(self):
        for client in ("pi", "omp"):
            with self.subTest(client=client):
                events = [
                    {"type": "session", "id": "s"},
                    {"type": "message", "id": "a", "message": {"role": "assistant", "usage": {"input": 30, "cacheRead": 50},
                        "contextSnapshot": {"promptTokens": 80, "nonMessageTokens": 40, "compactionEpoch": 0},
                        "content": [{"type": "toolCall", "id": "r", "name": "read", "arguments": {"path": "skill://test"}}]}},
                    {"type": "message", "message": {"role": "toolResult", "toolCallId": "r", "content": [{"type": "text", "text": "Skill instructions"}]}},
                    {"type": "compaction", "summary": "Summary"},
                ]
                report = analyze(self.session(events), client)
                self.assertEqual(report["client"], client)
                self.assertEqual(report["attributed_skill_returned_tokens"], tokens("Skill instructions"))
                self.assertEqual(report["context_snapshots"][0]["nonMessageTokens"], 40)
                self.assertEqual(len(report["compactions"]), 1)

    def test_claude_native_skill_counts_injected_body_not_launch_acknowledgement(self):
        events = [
            {"type": "assistant", "message": {"role": "assistant", "content": [{"type": "tool_use", "id": "s", "name": "Skill", "input": {"skill": "review"}}]}},
            {"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "s", "content": "Launching skill: review"}]}},
            {"type": "user", "isMeta": True, "sourceToolUseID": "s", "uuid": "injection", "message": {
                "role": "user", "content": [{"type": "text", "text": "Historical skill body"}]}},
        ]
        report = analyze(self.session(events))
        self.assertEqual(report["attributed_skill_returned_tokens"], tokens("Historical skill body"))
        self.assertEqual(report["skill_loads"][1]["evidence"], "linked-skill-injection")

    def test_codex_mixed_output_not_falsely_attributed(self):
        events = [
            {"type": "session_meta", "payload": {}},
            {"type": "response_item", "payload": {"type": "function_call", "call_id": "c", "name": "exec_command", "arguments": json.dumps({"cmd": "cat /skills/test/SKILL.md; ls /tmp"})}},
            {"type": "response_item", "payload": {"type": "function_call_output", "call_id": "c", "output": "Skill text plus file listing"}},
            {"type": "token_usage_record", "payload": {"response_id": "r", "usage": {"input_tokens": 100}}},
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 100}}}},
        ]
        report = analyze(self.session(events))
        self.assertEqual(report["client"], "codex")
        self.assertEqual(report["ambiguous_skill_outputs"], 1)
        self.assertEqual(report["attributed_skill_returned_tokens"], 0)
        self.assertEqual(report["reported_request_usage"]["input_tokens"], 100)

    def test_failed_read_is_not_skill_load(self):
        events = [
            {"type": "message", "message": {"role": "assistant", "content": [{"type": "toolCall", "id": "x", "name": "read", "arguments": {"path": "/skill/SKILL.md"}}]}},
            {"type": "message", "message": {"role": "toolResult", "toolCallId": "x", "isError": True, "content": [{"type": "text", "text": "not found"}]}}
        ]
        report = analyze(self.session(events), "pi")
        self.assertEqual(report["attributed_skill_returned_tokens"], 0)
        self.assertEqual(report["skills"][0]["failed_reads"], 1)

    def test_hook_context_and_malformed_record(self):
        path = self.session([{"type": "system", "hookAdditionalContext": ["Injected project card"]}])
        with path.open("a") as stream:
            stream.write("\n{bad")
        report = analyze(path)
        self.assertEqual(report["hook_context"][0]["tokens"], tokens("Injected project card"))
        self.assertEqual(len(report["errors"]), 1)
        self.assertNotIn("Injected project card", json.dumps(report))

    def test_discover_clients(self):
        for name, suffix in (("pi", ".pi/agent/sessions"), ("omp", ".omp/agent/sessions"), ("codex", ".codex/sessions"), ("claude", ".claude/projects")):
            folder = self.root / suffix
            folder.mkdir(parents=True)
            (folder / "s.jsonl").write_text("{}")
        self.assertEqual(len(discover(self.root)), 4)
        self.assertEqual(discover(self.root, "omp")[0]["client"], "omp")
