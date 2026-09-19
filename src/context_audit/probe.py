"""Run installed clients against a local rejecting sink, never a model provider."""
import json
import os
import shutil
import signal
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .capture import CaptureStore
from .recordings import session_identity


def probe(client, directory, timeout=30):
    executable = shutil.which(client)
    if not executable:
        return dict(client=client, status="unavailable")
    store = CaptureStore(directory, client)
    received = threading.Event()
    summary = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            size = int(self.headers.get("Content-Length", "0"))
            if size > 32_000_000:
                self.send_error(413)
                return
            try:
                data = json.loads(self.rfile.read(size))
                session_id, evidence = session_identity(client, data, self.headers)
                row = store.request(data, session_id=session_id, session_evidence=evidence)
                summary.append(dict(request_id=row["request_id"], components=len(row["components"]),
                                    categories=sorted(set(x["category"] for x in row["components"])),
                                    estimated_tokens=row["serialized_request_tokens"]))
                received.set()
            except (ValueError, TypeError):
                pass
            self.send_response(400)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"type":"error","error":{"type":"invalid_request_error","message":"Context Audit local capture probe finished; no inference performed."}}')

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    env = os.environ.copy()
    for key in list(env):
        if key.startswith(("ANTHROPIC_", "CLAUDE_CODE_", "OPENAI_")) or key in ("CLAUDECODE", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            env.pop(key, None)
    endpoint = f"http://127.0.0.1:{server.server_port}"
    with tempfile.TemporaryDirectory(prefix="context-audit-probe-") as tmp:
        # A distinctive prompt is kept in memory only by our capture artifacts.
        prompt = "Context Audit capture probe. Reply OK. Do not use tools."
        if client == "claude":
            env.update(ANTHROPIC_API_KEY="context-audit-dummy", ANTHROPIC_BASE_URL=endpoint,
                       CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1")
            argv = [executable, "--bare", "-p", prompt, "--no-session-persistence", "--setting-sources", "",
                    "--strict-mcp-config", "--model", "claude-sonnet-4-6", "--tools", "Read"]
        else:
            argv = [executable, "exec", "--ignore-user-config", "--ephemeral", "--skip-git-repo-check",
                    "-c", 'model_provider="audit_probe"', "-c", 'model_providers.audit_probe.name="Audit local probe"',
                    "-c", f'model_providers.audit_probe.base_url="{endpoint}/v1"',
                    "-c", 'model_providers.audit_probe.wire_api="responses"',
                    "-c", 'model_providers.audit_probe.request_max_retries=0',
                    "-c", 'model_providers.audit_probe.stream_max_retries=0',
                    "-c", 'model="gpt-5.6-sol"', prompt]
        process = subprocess.Popen(argv, cwd=tmp, env=env, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + timeout
        try:
            while not received.wait(.1) and process.poll() is None and time.monotonic() < deadline:
                pass
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()
            server.shutdown()
            server.server_close()
    result = dict(client=client, status="captured" if received.is_set() else "no-request-captured",
                  observations=summary, artifact=str(directory / "events.jsonl"),
                  validation_scope="Local endpoint capture only; normal authentication and successful inference are not tested.")
    return result
