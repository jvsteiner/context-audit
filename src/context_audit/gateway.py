"""Opt-in loopback HTTP gateway. No TLS interception, request-body disk logging,
redirect following, or WebSocket fallback. Only a fixed upstream is reachable.
"""
import json
import secrets
import threading
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .capture import CaptureStore
from .recordings import session_identity


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Gateway:
    def __init__(self, upstream, store: CaptureStore, port=0, prefix=None, event_stores=None):
        parsed = urllib.parse.urlsplit(upstream)
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("Upstream must be a base URL without credentials, query or fragment")
        if parsed.scheme != "https" and not (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost", "::1")):
            raise ValueError("Upstream must use HTTPS, or loopback HTTP for tests")
        self.upstream = upstream.rstrip("/")
        self.store = store
        self.prefix = prefix or "/" + secrets.token_urlsafe(24)
        gateway = self
        hop = {"host", "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade", "content-length", "accept-encoding"}

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_GET(self):
                if self.path == gateway.prefix + '/health':
                    body = json.dumps({'service': 'context-audit', 'client': gateway.store.client}).encode()
                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.send_header('Content-Length', str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                    return
                # Codex refreshes its model catalog independently of inference.
                # This is not a context request. Keep auth in memory and never
                # follow redirects or open arbitrary GET routes.
                if (urllib.parse.urlsplit(self.path).path == gateway.prefix + '/models'
                        and self.headers.get('Upgrade', '').lower() != 'websocket'):
                    headers = {k: v for k, v in self.headers.items() if k.lower() not in hop}
                    headers['Accept-Encoding'] = 'identity'
                    request = urllib.request.Request(gateway.upstream + self.path[len(gateway.prefix):], headers=headers)
                    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
                    try:
                        try:
                            response = opener.open(request, timeout=30)
                        except urllib.error.HTTPError as exc:
                            response = exc
                        with response:
                            self.send_response(response.status)
                            for key, value in response.headers.items():
                                if key.lower() not in hop:
                                    self.send_header(key, value)
                            self.send_header('Connection', 'close')
                            self.end_headers()
                            while chunk := response.read(65536):
                                self.wfile.write(chunk)
                    except (OSError, urllib.error.URLError):
                        self.close_connection = True
                    return
                # Never silently miss a WebSocket request.
                self.send_error(501, "HTTP JSON POST capture only; WebSocket capture is not supported")

            def do_POST(self):
                if not self.path.startswith(gateway.prefix + "/"):
                    self.send_error(404)
                    return
                if self.headers.get("Transfer-Encoding") or self.headers.get("Content-Encoding", "identity") != "identity":
                    self.send_error(415, "Compressed or chunked requests are not supported")
                    return
                try:
                    size = int(self.headers.get("Content-Length", "0"))
                    if not 0 < size <= 32_000_000:
                        self.send_error(413)
                        return
                    raw = self.rfile.read(size)
                    payload = json.loads(raw)
                    if not isinstance(payload, dict):
                        raise ValueError('Expected JSON object')
                    if self.path == gateway.prefix + '/events':
                        from .recordings import valid_id
                        from .provenance import record_event
                        client, sid = payload.get('client'), payload.get('session_id')
                        if not event_stores or client not in event_stores or not valid_id(sid):
                            raise ValueError('Invalid collector client or session')
                        target = event_stores[client].for_session(sid)
                        if payload.get('event') in ('provider_request', 'runtime_context'):
                            if client not in ('omp', 'pi'):
                                raise ValueError('Native provider capture is for extension clients')
                            target.request(payload['payload'], transport='native-extension', session_id=sid,
                                           session_evidence='native-session-manager',
                                           record_type='runtime-context' if payload['event'] == 'runtime_context' else 'request')
                        else:
                            record_event(target, payload['payload'])
                        self.send_response(204)
                        self.end_headers()
                        return
                    session_id, evidence = session_identity(gateway.store.client, payload, self.headers)
                    store = gateway.store.for_session(session_id) if hasattr(gateway.store, 'for_session') else gateway.store
                    row = store.request(payload, session_id=session_id, session_evidence=evidence)
                except (ValueError, OSError, TypeError, KeyError):
                    self.send_error(400, "Request could not be captured; not forwarded")
                    return
                headers = {k: v for k, v in self.headers.items() if k.lower() not in hop}
                headers["Accept-Encoding"] = "identity"
                url = gateway.upstream + self.path[len(gateway.prefix):]
                request = urllib.request.Request(url, raw, headers, method="POST")
                # Ignore ambient proxy configuration; the upstream was selected explicitly.
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
                try:
                    response = opener.open(request, timeout=120)
                except urllib.error.HTTPError as exc:
                    response = exc
                except (OSError, urllib.error.URLError):
                    self.send_error(502, "Upstream connection failed")
                    return
                usage = []
                pending = b""
                complete = False
                try:
                    with response:
                        self.send_response(response.status)
                        for key, value in response.headers.items():
                            if key.lower() not in hop:
                                self.send_header(key, value)
                        self.send_header("Connection", "close")
                        self.end_headers()
                        while True:
                            chunk = response.read1(65536)
                            if not chunk:
                                complete = True
                                break
                            self.wfile.write(chunk)
                            self.wfile.flush()
                            pending += chunk
                            while b"\n" in pending:
                                line, pending = pending.split(b"\n", 1)
                                if line.startswith(b"data:"):
                                    self.collect_usage(line[5:].strip(), usage)
                            if len(pending) > 2_000_000:
                                pending = b""  # bounded memory; usage coverage is best effort
                        self.collect_usage(pending, usage)
                except OSError:
                    pass
                finally:
                    self.close_connection = True
                    with store.lock:
                        store.append(dict(type="response", request_id=row["request_id"],
                                                  status=response.status, stream_complete=complete,
                                                  usage_events=usage, usage_capture="best-effort"))

            @staticmethod
            def collect_usage(raw, destination):
                try:
                    event = json.loads(raw)
                except (ValueError, UnicodeError):
                    return
                if not isinstance(event, dict):
                    return
                for container in (event, event.get("message"), event.get("response")):
                    if isinstance(container, dict) and isinstance(container.get("usage"), dict):
                        # Preserve numeric counters only; provider output is discarded.
                        destination.append({k: v for k, v in container["usage"].items()
                                            if isinstance(v, int) and not isinstance(v, bool)})

        class QuietServer(ThreadingHTTPServer):
            def handle_error(self, request, client_address):
                # Do not put request data, headers or exception values into daemon logs.
                pass
        self.server = QuietServer(("127.0.0.1", port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}{self.prefix}"

    def start(self):
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=3)
