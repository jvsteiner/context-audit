import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from context_audit.capture import CaptureStore
from context_audit.gateway import Gateway


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = CaptureStore(self.root, "claude")

    def test_no_text_persisted_and_every_field_represented(self):
        secret = "PRIVATE_PROMPT_VALUE_123456"
        source = self.store.source("hook", "SessionStart/test", secret)
        row = self.store.request({"system": [{"type": "text", "text": secret}], "messages": [],
                                  "tools": [], "metadata": {"auth": secret}})
        self.assertNotIn(secret, (self.root / "events.jsonl").read_text())
        self.assertEqual({c["pointer"] for c in row["components"]}, {"/system/0", "/messages", "/tools", "/metadata"})
        self.assertEqual(row["components"][0]["source_ids"], [source["source_id"]])
        self.assertEqual((self.root / "fingerprint.key").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.root / "events.jsonl").stat().st_mode & 0o777, 0o600)

    def test_deltas_repeated_components_and_server_references(self):
        self.store.request({"messages": ["a", "a", "b"]})
        row = self.store.request({"messages": ["a", "c"], "previous_response_id": "not-persisted"})
        self.assertEqual(row["delta"], {"added": 2, "removed": 2, "retained": 1})
        self.assertEqual(row["context_status"], "server-reference-unresolved")
        self.assertNotIn("not-persisted", (self.root / "events.jsonl").read_text())

    def test_key_is_local_and_stable(self):
        self.store.source("instructions", "test.md", "text")
        self.store.request({"instructions": "text"})
        other = CaptureStore(self.root / "other", "codex")
        reopened = CaptureStore(self.root, "claude")
        self.assertEqual(self.store.fingerprint("text"), reopened.fingerprint("text"))
        self.assertNotEqual(self.store.fingerprint("text"), other.fingerprint("text"))
        resumed = reopened.request({"instructions": "text"})
        self.assertEqual(resumed["sequence"], 2)
        self.assertEqual(resumed["delta"]["retained"], 1)
        self.assertTrue(resumed["components"][0]["source_ids"])

    def test_gateway_forwards_unchanged_and_retains_only_usage(self):
        seen = []
        body = b'data: {"type":"message_delta","usage":{"output_tokens":7},"text":"PRIVATE_OUTPUT"}\n\n'

        class Upstream(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                seen.append((self.path, self.headers.get("Authorization"), self.rfile.read(int(self.headers["Content-Length"]))))
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        threading.Thread(target=upstream.serve_forever, daemon=True).start()
        gateway = Gateway(f"http://127.0.0.1:{upstream.server_port}", self.store)
        gateway.start()
        try:
            data = b'{"system":"PRIVATE_SYSTEM","messages":[],"tools":[]}'
            request = urllib.request.Request(gateway.url + "/v1/messages", data, {"Authorization": "Bearer PRIVATE_CREDENTIAL", "Content-Type": "application/json"})
            with urllib.request.urlopen(request) as response:
                self.assertEqual(response.read(), body)
            self.assertEqual(seen, [("/v1/messages", "Bearer PRIVATE_CREDENTIAL", data)])
        finally:
            gateway.close()
            upstream.shutdown()
            upstream.server_close()
        artifacts = (self.root / "events.jsonl").read_text()
        for secret in ("PRIVATE_SYSTEM", "PRIVATE_OUTPUT", "PRIVATE_CREDENTIAL"):
            self.assertNotIn(secret, artifacts)
        records = [json.loads(x) for x in artifacts.splitlines()]
        self.assertEqual(records[-1]["usage_events"], [{"output_tokens": 7}])
        self.assertTrue(records[-1]["stream_complete"])

    def test_gateway_rejects_credential_urls(self):
        with self.assertRaises(ValueError):
            Gateway("https://user:password@example.com", self.store)
