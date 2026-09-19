"""Per-process capture routing; never edits client config or persists credentials."""
import json
import os
import subprocess
import uuid
from pathlib import Path

from .capture import CaptureStore
from .gateway import Gateway


def command(client, endpoint, auth, args, env):
    env = dict(env)
    if client == 'claude':
        if any(env.get(k) for k in ('CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY')):
            raise ValueError('This launcher supports the direct Anthropic endpoint only, not Bedrock/Vertex/Foundry.')
        env['ANTHROPIC_BASE_URL'] = endpoint
        return ['claude', *args], env
    if auth not in ('chatgpt', 'api-key'):
        raise ValueError('Codex requires an explicit --auth chatgpt or --auth api-key.')
    settings = {'model_provider': 'context_audit', 'model_providers.context_audit.name': 'Context Audit HTTP capture',
                'model_providers.context_audit.base_url': endpoint,
                'model_providers.context_audit.wire_api': 'responses',
                'model_providers.context_audit.supports_websockets': False,
                'model_providers.context_audit.requires_openai_auth': auth == 'chatgpt'}
    if auth == 'api-key':
        settings['model_providers.context_audit.env_key'] = 'OPENAI_API_KEY'
    overrides = [part for key, value in settings.items() for part in ('-c', key + '=' + json.dumps(value))]
    return ['codex', *overrides, *args], env


def run(client, upstream, auth, args, root=None):
    root = root or Path.home() / '.context-audit/captures'
    directory = root / (client + '-' + str(uuid.uuid4()))
    # Validate before starting any subprocess. Gateway also validates upstream safety.
    command(client, 'http://127.0.0.1', auth, args, os.environ)
    gateway = Gateway(upstream, CaptureStore(directory, client))
    argv, env = command(client, gateway.url, auth, args, os.environ)
    gateway.start()
    print(f'Recording received HTTP requests to {directory}', flush=True)
    print('Capture is experimental: source attribution and traffic coverage are not yet complete.', flush=True)
    try:
        result = subprocess.run(argv, env=env)
        return result.returncode
    finally:
        gateway.close()
        from .recordings import requests
        rows = requests(directory)
        print(f'Captured {len(rows)} requests; {sum(bool(r.get("session_id")) for r in rows)} have session identity.', flush=True)
        if not rows:
            print('NOT CAPTURED: no requests reached the recorder. No audit is available.', flush=True)
