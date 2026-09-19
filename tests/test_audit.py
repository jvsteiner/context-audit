import json
import tempfile
import unittest
from pathlib import Path

from context_audit.cli import disable_hook, restore
from context_audit.core import audit_tools, compare, observe, scan, tokens


class AuditTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.project = self.root / "project"
        self.home.mkdir()
        self.project.mkdir()
        (self.project / ".git").mkdir()

    def write(self, path, text):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        return path

    def hook_config(self):
        return self.write(self.home / ".claude/settings.json", json.dumps({
            "hooks": {"SessionStart": [{"hooks": [{"type": "command", "command": "echo TOP_SECRET"}]}]},
            "env": {"API_KEY": "PRIVATE_VALUE"}}))

    def test_inventory_excludes_secrets_and_marks_unknown_runtime(self):
        self.hook_config()
        self.write(self.home / ".codex/config.toml", '[mcp_servers.example]\ncommand = "example"\n[mcp_servers.example.env]\nTOKEN = "PRIVATE_VALUE"\n')
        report = scan(self.project, self.home)
        serialized = json.dumps(report)
        self.assertNotIn("PRIVATE_VALUE", serialized)
        self.assertNotIn("TOP_SECRET", serialized)
        hook = next(x for x in report["items"] if x["kind"] == "hook")
        self.assertIsNone(hook["details"]["runtime_output_tokens"])
        server = next(x for x in report["items"] if x["kind"] == "mcp-server")
        self.assertIsNone(server["details"]["schema_tokens"])

    def test_codex_override_and_git_boundary(self):
        self.write(self.root / "AGENTS.md", "outside the git boundary")
        self.write(self.project / "AGENTS.md", "normal instructions")
        override = self.write(self.project / "AGENTS.override.md", "override instructions")
        report = scan(self.project, self.home)
        instructions = [x for x in report["items"] if x["kind"] == "instructions"]
        self.assertEqual([x["path"] for x in instructions], [str(override.resolve())])

    def test_skill_metadata_is_separate_from_body(self):
        self.write(self.home / ".agents/skills/test/SKILL.md", '---\nname: test\ndescription: Find test cases\n---\n' + "Large body. " * 1000)
        report = scan(self.project, self.home)
        by_kind = {x["kind"]: x for x in report["items"]}
        self.assertGreater(by_kind["skill-body"]["tokens"], by_kind["skill-metadata"]["tokens"])
        self.assertEqual(by_kind["skill-body"]["loading"], "on-demand")
        self.assertNotIn("startup-candidate", report["totals_by_loading"])

    def test_malformed_config_is_reported_not_silently_skipped(self):
        self.write(self.home / ".claude/settings.json", "{broken")
        self.assertEqual(len(scan(self.project, self.home)["errors"]), 1)

    def test_diff_detects_hook_change_even_same_length(self):
        path = self.hook_config()
        before = scan(self.project, self.home)
        self.write(path, path.read_text().replace("echo", "printf"))
        after = scan(self.project, self.home)
        delta = compare(before, after)
        self.assertTrue(any(x["after"]["kind"] == "hook" for x in delta["changed"]))

    def test_tool_audit_handles_rpc_envelope(self):
        report = audit_tools({"result": {"tools": [{"name": "search", "description": "", "inputSchema": {
            "type": "object", "properties": {"query": {"type": "string"}}}}]}})
        self.assertIn("Missing description", report["tools"][0]["findings"])
        self.assertGreater(report["serialized_tokens"], 0)

    def test_usage_does_not_sum_cumulative_or_duplicate_messages(self):
        events = [
            {"type": "event_msg", "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": n}}}}
            for n in [10, 20, 20]]
        events += [{"type": "assistant", "message": {"id": "same", "usage": {"input_tokens": 4}}}] * 2
        path = self.write(self.root / "session.jsonl", "\n".join(json.dumps(x) for x in events))
        report = observe([path])
        self.assertEqual(report["codex_latest_cumulative_by_file"][str(path)]["input_tokens"], 20)
        self.assertEqual(report["claude_reported_usage"]["input_tokens"], 4)

    def test_hook_preview_apply_restore_and_conflict(self):
        path = self.hook_config()
        before = path.read_text()
        self.assertFalse(disable_hook(path, "SessionStart", 0, 0, False)["applied"])
        self.assertEqual(before, path.read_text())
        result = disable_hook(path, "SessionStart", 0, 0, True)
        receipt = Path(result["receipt"])
        self.assertEqual(json.loads(path.read_text())["hooks"]["SessionStart"], [])
        restore(receipt)
        self.assertEqual(before, path.read_text())
        result = disable_hook(path, "SessionStart", 0, 0, True)
        self.write(path, path.read_text() + " ")
        with self.assertRaisesRegex(ValueError, "changed since"):
            restore(Path(result["receipt"]))

    def test_special_tokens_are_counted_as_text(self):
        self.assertGreater(tokens("<|endoftext|>"), 0)


if __name__ == "__main__":
    unittest.main()
