"""Exact session associations from request metadata, never newest-file guesses."""
import json
import re
from pathlib import Path


def valid_id(value):
    return isinstance(value, str) and bool(re.fullmatch(r'[a-zA-Z0-9_-]{1,128}', value))


def has_tool_definitions(request):
    return any(c.get('category') == 'tool-definition' for c in request.get('components', []))


def session_identity(client, payload, headers):
    if client == 'codex':
        # Codex session-id is transport identity; thread-id matches CODEX_THREAD_ID.
        value = headers.get('thread-id')
        metadata = payload.get('client_metadata', {}) if isinstance(payload, dict) else {}
        recorded = metadata.get('thread_id') if isinstance(metadata, dict) else None
        if valid_id(value) and valid_id(recorded) and value != recorded:
            return None, None
        if valid_id(value):
            return value, 'request-header:thread-id'
        return (recorded, 'request-metadata:thread_id') if valid_id(recorded) else (None, None)
    if client == 'claude':
        metadata = payload.get('metadata', {}) if isinstance(payload, dict) else {}
        user_id = metadata.get('user_id') if isinstance(metadata, dict) else None
        try:
            parsed = json.loads(user_id) if isinstance(user_id, str) else {}
        except ValueError:
            parsed = {}
        value = parsed.get('session_id') if isinstance(parsed, dict) else None
        if not value and isinstance(user_id, str):
            match = re.search(r'_session_([a-zA-Z0-9-]+)$', user_id)
            value = match[1] if match else None
        return (value, 'request-metadata:session_id') if valid_id(value) else (None, None)
    return None, None


def requests(directory):
    path = directory / 'events.jsonl'
    if not path.exists():
        return []
    # A live append can leave a final line unfinished. Never ignore corrupt complete lines.
    lines = path.read_text().splitlines(keepends=True)
    return [row for line in lines if line.endswith('\n')
            if (row := json.loads(line)).get('type') == 'request']


def discover(root=None):
    root = root or Path.home() / '.context-audit/captures'
    found = []
    for path in sorted(root.glob('*/events.jsonl')):
        rows = requests(path.parent)
        identities = sorted({(r['client'], r.get('session_id') or '') for r in rows})
        for client, session_id in identities:
            matches = [r for r in rows if r['client'] == client and (r.get('session_id') or '') == session_id]
            found.append(dict(directory=str(path.parent), client=client, session_id=session_id or None,
                              requests=len(matches), latest=matches[-1]['timestamp']))
    return found


def resolve(client, session_id, root=None):
    if not valid_id(session_id):
        raise ValueError('Active session identity is unavailable; no capture will be guessed.')
    matches = [r for r in discover(root) if r['client'] == client and r['session_id'] == session_id]
    if not matches:
        raise ValueError('This session was not captured. No audit graph was generated. '
                         'Check context-audit status and enable default recording with context-audit install, or use context-audit run. '
                         'For partial transcript exploration, use context-audit visualize explicitly.')
    if len(matches) != 1:
        raise ValueError('Multiple recordings match this session. Use captures and capture-report to select one explicitly.')
    return Path(matches[0]['directory'])
