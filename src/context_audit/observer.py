"""Silent, non-decision-making hook observer. Raw event JSON only travels locally."""
import json
import sys
import urllib.request
from pathlib import Path


def submit(client, payload, root=None):
    root = root or Path.home() / '.context-audit'
    config = json.loads((root / 'service.json').read_text())
    route = config['routes'].get(client, config['routes']['codex'])
    url = f"http://127.0.0.1:{route['port']}{route['prefix']}/events"
    envelope = dict(client=client, session_id=payload.get('session_id'), payload=payload)
    raw = json.dumps(envelope).encode()
    if len(raw) > 32_000_000:
        raise ValueError('Observer payload exceeds limit')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(urllib.request.Request(url, raw, {'Content-Type': 'application/json'}), timeout=2) as response:
        if response.status != 204:
            raise ValueError('Observer rejected')


def observe_stdin(client):
    try:
        raw = sys.stdin.buffer.read(32_000_001)
        if len(raw) > 32_000_000:
            raise ValueError('Observer payload exceeds limit')
        submit(client, json.loads(raw))
    except Exception:
        # Never return a blocking hook status, decisions, or context on stdout.
        print('context-audit: lifecycle observation unavailable', file=sys.stderr)
