"""Previewed, backed-up macOS login-service installation and guarded restore."""
import hashlib
import json
import os
import plistlib
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from collections import Counter
from pathlib import Path

import tomlkit

LABEL = 'local.context-audit.recorder'


def fingerprint(text):
    return hashlib.sha256(text.encode()).hexdigest()


def current(path):
    if path.is_symlink():
        raise ValueError(f'Refusing a symlink configuration target: {path}')
    return path.read_text() if path.exists() else None


def free_ports(count):
    sockets = [socket.socket() for _ in range(count)]
    try:
        for sock in sockets:
            sock.bind(('127.0.0.1', 0))
        return [sock.getsockname()[1] for sock in sockets]
    finally:
        for sock in sockets:
            sock.close()


def endpoint(route):
    return f"http://127.0.0.1:{route['port']}{route['prefix']}"


def detect_codex_auth():
    result = subprocess.run(['codex', 'login', 'status'], capture_output=True, text=True)
    summary = result.stdout + result.stderr
    if result.returncode == 0 and 'ChatGPT' in summary:
        return 'chatgpt'
    if result.returncode == 0 and 'API key' in summary:
        return 'api-key'
    raise ValueError('Could not determine Codex login method. Pass --codex-auth chatgpt or --codex-auth api-key explicitly.')


def plan(home=None, project=None, codex_auth=None):
    codex_auth = codex_auth or detect_codex_auth()
    if codex_auth not in ('chatgpt', 'api-key'):
        raise ValueError('Select Codex authentication: chatgpt or api-key')
    home = home or Path.home()
    project = project or Path(__file__).resolve().parents[2]
    root = home / '.context-audit'
    receipt = root / 'installation.json'
    if receipt.exists() and json.loads(receipt.read_text()).get('state') != 'uninstalled':
        raise ValueError('An installation already exists. Use status or uninstall before changing it.')
    codex_home = Path(os.environ.get('CODEX_HOME', str(home / '.codex'))) if home == Path.home() else home / '.codex'
    paths = {'codex': codex_home / 'config.toml', 'claude': home / '.claude/settings.json'}
    before = {client: current(path) for client, path in paths.items()}
    codex = tomlkit.parse(before['codex'] or '')
    claude = json.loads(before['claude'] or '{}')
    if not isinstance(claude, dict) or not isinstance(claude.get('env', {}), dict):
        raise ValueError('Claude settings and env must be JSON objects')
    if codex.get('model_provider', 'openai') != 'openai' or any(k in codex for k in ('openai_base_url', 'chatgpt_base_url')):
        raise ValueError('Existing Codex provider/endpoint override requires an explicit migration; not overwritten.')
    if 'context_audit' in codex.get('model_providers', {}):
        raise ValueError('Codex provider context_audit already exists; not overwritten.')
    routing = ('ANTHROPIC_BASE_URL', 'CLAUDE_CODE_USE_BEDROCK', 'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY')
    if any(k in claude.get('env', {}) or os.environ.get(k) for k in routing) or os.environ.get('OPENAI_BASE_URL'):
        raise ValueError('Existing environment routing requires an explicit migration; not overwritten.')
    ports = free_ports(2)
    routes = {client: dict(port=ports[i], prefix='/' + secrets.token_urlsafe(24), upstream=upstream)
              for i, (client, upstream) in enumerate((('codex', 'https://chatgpt.com/backend-api/codex' if codex_auth == 'chatgpt' else 'https://api.openai.com/v1'),
                                                      ('claude', 'https://api.anthropic.com')))}
    config = dict(version=1, routes=routes, captures=str(root / 'captures'))
    codex['model_provider'] = 'context_audit'
    if 'model_providers' not in codex:
        codex['model_providers'] = tomlkit.table()
    provider = dict(name='Context Audit recorder', base_url=endpoint(routes['codex']), wire_api='responses',
                    supports_websockets=False, requires_openai_auth=True)
    # requires_openai_auth preserves the client's saved OpenAI login; never reads auth files.
    codex['model_providers']['context_audit'] = provider
    claude.setdefault('env', {})['ANTHROPIC_BASE_URL'] = endpoint(routes['claude'])
    service_path = root / 'service.json'
    plist_path = home / 'Library/LaunchAgents' / (LABEL + '.plist')
    if service_path.exists() or plist_path.exists():
        raise ValueError('Recorder service files already exist without an active receipt; refusing to replace them.')
    plist = dict(Label=LABEL, ProgramArguments=[sys.executable, '-c', 'from context_audit.cli import main; main()', 'service', '--config', str(service_path)],
                 WorkingDirectory=str(project), RunAtLoad=True, KeepAlive=True, ThrottleInterval=10,
                 StandardOutPath='/dev/null', StandardErrorPath='/dev/null', Umask=63)
    edits = [dict(path=str(paths[c]), before=before[c], after=after, purpose=c + ' default routing')
             for c, after in (('codex', tomlkit.dumps(codex)), ('claude', json.dumps(claude, indent=2) + '\n'))]
    edits += [dict(path=str(service_path), before=None, after=json.dumps(config, indent=2) + '\n', purpose='recorder endpoints'),
              dict(path=str(plist_path), before=None, after=plistlib.dumps(plist).decode(), purpose='macOS login service')]
    from .integrations import integration_files
    for path, text in integration_files(home, project).items():
        existing = current(path)
        if existing is not None and existing != text:
            raise ValueError(f'Existing integration differs; refusing to overwrite: {path}')
        if existing is None:
            edits.append(dict(path=str(path), before=None, after=text, purpose='in-session command'))
    return dict(home=str(home), root=str(root), config=config, edits=edits, receipt=str(receipt), plist=str(plist_path))


def preview(proposal):
    return dict(action='install', changes=[{'path': e['path'], 'purpose': e['purpose'], 'existing': e['before'] is not None} for e in proposal['edits']],
                captures=proposal['config']['captures'],
                behavior='Starts at login; routes ordinary Claude/Codex launches through local HTTP recording. If recorder is down, routed requests fail.',
                limitations='Experimental HTTP-only capture; CLI/profile/project overrides can bypass routing. Pi/OMP, desktop validation and full injection provenance remain incomplete.')


def launchctl(*args):
    result = subprocess.run(['launchctl', *args], capture_output=True, text=True)
    if result.returncode:
        raise ValueError('launchctl ' + args[0] + ' failed; client configuration was not silently accepted.')


def healthy(route, client):
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open(endpoint(route) + '/health', timeout=1) as response:
            return json.load(response) == {'service': 'context-audit', 'client': client}
    except (OSError, ValueError):
        return False


def apply(proposal):
    from .cli import atomic_write
    if sys.platform != 'darwin':
        raise ValueError('Default-on login service currently supports macOS only; no files changed.')
    for edit in proposal['edits']:
        if current(Path(edit['path'])) != edit['before']:
            raise ValueError('Configuration changed after preview; refusing to overwrite.')
    root = Path(proposal['root'])
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup_dir = root / 'backups' / str(uuid.uuid4())
    backup_dir.mkdir(parents=True, mode=0o700)
    receipt = dict(version=1, state='installing', plist=proposal['plist'], files=[])
    receipt_path = Path(proposal['receipt'])
    for i, edit in enumerate(proposal['edits']):
        backup = backup_dir / str(i)
        if edit['before'] is not None:
            atomic_write(backup, edit['before'])
        receipt['files'].append(dict(path=edit['path'], existed=edit['before'] is not None, backup=str(backup),
                                     before_hash=fingerprint(edit['before']) if edit['before'] is not None else None,
                                     after_hash=fingerprint(edit['after'])))
    atomic_write(receipt_path, json.dumps(receipt, indent=2))
    written = []
    started = False
    try:
        # Bring service up first. Never route clients to an unverified recorder.
        for edit in proposal['edits'][2:]:
            path = Path(edit['path'])
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write(path, edit['after'])
            written.append(edit)
        launchctl('bootstrap', f'gui/{os.getuid()}', proposal['plist'])
        started = True
        deadline = time.monotonic() + 10
        while not all(healthy(route, client) for client, route in proposal['config']['routes'].items()):
            if time.monotonic() > deadline:
                raise ValueError('Recorder did not become healthy; rolling back installation.')
            time.sleep(0.2)
        for edit in proposal['edits'][:2]:
            path = Path(edit['path'])
            if current(path) != edit['before']:
                raise ValueError('Client configuration changed during installation; rolling back.')
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write(path, edit['after'])
            written.append(edit)
        receipt['state'] = 'installed'
        atomic_write(receipt_path, json.dumps(receipt, indent=2))
    except Exception:
        conflicts = []
        for edit in reversed(written):
            path = Path(edit['path'])
            if current(path) != edit['after']:
                conflicts.append(str(path))
                continue
            if edit['before'] is None:
                path.unlink()
            else:
                atomic_write(path, edit['before'])
        if started:
            try:
                launchctl('bootout', f'gui/{os.getuid()}/{LABEL}')
            except ValueError:
                conflicts.append('login service could not be stopped')
        receipt['state'] = 'rollback-conflict' if conflicts else 'uninstalled'
        receipt['conflicts'] = conflicts
        atomic_write(receipt_path, json.dumps(receipt, indent=2))
        raise
    return dict(installed=True, receipt=str(receipt_path), captures=proposal['config']['captures'])


def status(home=None):
    root = (home or Path.home()) / '.context-audit'
    receipt_path = root / 'installation.json'
    if not receipt_path.exists():
        return dict(installed=False, message='Default recording is not installed.')
    receipt = json.loads(receipt_path.read_text())
    config_path = root / 'service.json'
    config = json.loads(config_path.read_text()) if config_path.exists() else {'routes': {}}
    routing = {}
    for i, client in enumerate(('codex', 'claude')):
        try:
            restore_client(receipt['files'][i], client, config)
            routing[client] = True
        except (ValueError, OSError, KeyError):
            routing[client] = False
    return dict(state=receipt['state'], clients={c: {'recorder_healthy': healthy(r, c), 'default_routing_installed': routing[c]} for c, r in config['routes'].items()},
                config_unchanged=all(current(Path(e['path'])) is not None and fingerprint(current(Path(e['path']))) == e['after_hash'] for e in receipt['files']),
                message='Health is not proof that every client request was captured or every source attributed.')


def restore_client(edit, client, config):
    """Check only owned routing fields; preserve client/user updates elsewhere."""
    value = current(Path(edit['path']))
    if value is None:
        raise ValueError(f"Configuration is missing: {edit['path']}")
    original = Path(edit['backup']).read_text() if edit['existed'] else ''
    if edit['existed'] and fingerprint(original) != edit['before_hash']:
        raise ValueError('Backup checksum mismatch; refusing restore.')
    if fingerprint(value) == edit['after_hash']:
        return original if edit['existed'] else None
    if client == 'codex':
        data, before = tomlkit.parse(value), tomlkit.parse(original)
        expected = dict(name='Context Audit recorder', base_url=endpoint(config['routes']['codex']), wire_api='responses',
                        supports_websockets=False, requires_openai_auth=True)
        if data.get('model_provider') != 'context_audit' or data.get('model_providers', {}).get('context_audit') != expected:
            raise ValueError('Codex routing changed since installation; refusing to overwrite it.')
        if 'model_provider' in before:
            data['model_provider'] = before['model_provider']
        else:
            del data['model_provider']
        del data['model_providers']['context_audit']
        if not data['model_providers'] and 'model_providers' not in before:
            del data['model_providers']
        rendered = tomlkit.dumps(data)
        # TOML associates trailing comments with tables. Preserve comments that
        # would otherwise disappear when removing our provider table.
        previous_comments = Counter(line for line in value.splitlines() if line.lstrip().startswith('#'))
        remaining_comments = Counter(line for line in rendered.splitlines() if line.lstrip().startswith('#'))
        missing = list((previous_comments - remaining_comments).elements())
        return rendered + ('\n' + '\n'.join(missing) + '\n' if missing else '')
    data, before = json.loads(value), json.loads(original or '{}')
    if data.get('env', {}).get('ANTHROPIC_BASE_URL') != endpoint(config['routes']['claude']):
        raise ValueError('Claude routing changed since installation; refusing to overwrite it.')
    del data['env']['ANTHROPIC_BASE_URL']
    if not data['env'] and 'env' not in before:
        del data['env']
    return json.dumps(data, indent=2) + '\n'


def uninstall(home=None, apply_changes=False):
    from .cli import atomic_write
    root = (home or Path.home()) / '.context-audit'
    receipt_path = root / 'installation.json'
    receipt = json.loads(receipt_path.read_text())
    if receipt['state'] == 'uninstalled':
        return dict(uninstalled=True, message='Already uninstalled; recordings retained.')
    config = json.loads((root / 'service.json').read_text())
    restored = [restore_client(e, c, config) for e, c in zip(receipt['files'][:2], ('codex', 'claude'))]
    observed = [current(Path(e['path'])) for e in receipt['files'][:2]]
    for edit in receipt['files'][2:]:
        value = current(Path(edit['path']))
        if value is None or fingerprint(value) != edit['after_hash']:
            raise ValueError(f"Configuration changed since installation; refusing to overwrite: {edit['path']}")
        if edit['existed'] and fingerprint(Path(edit['backup']).read_text()) != edit['before_hash']:
            raise ValueError('Backup checksum mismatch; refusing restore.')
    if not apply_changes:
        return dict(action='uninstall', restore=[e['path'] for e in receipt['files']], retain='All recordings, reports and private backups')
    # Restore routing before stopping the service.
    for index, edit in enumerate(receipt['files'][:2]):
        path = Path(edit['path'])
        if current(path) != observed[index]:
            raise ValueError('Configuration changed during restore; refusing to overwrite.')
        if restored[index] is not None:
            atomic_write(path, restored[index])
        else:
            path.unlink()
    try:
        launchctl('bootout', f'gui/{os.getuid()}/{LABEL}')
    except ValueError:
        check = subprocess.run(['launchctl', 'print', f'gui/{os.getuid()}/{LABEL}'], capture_output=True)
        if check.returncode == 0:
            # Keep the installation recoverable if launchd failed to stop it.
            for index, edit in enumerate(receipt['files'][:2]):
                path = Path(edit['path'])
                if current(path) == restored[index]:
                    atomic_write(path, observed[index])
            raise
    for edit in receipt['files'][2:]:
        Path(edit['path']).unlink()
    receipt['state'] = 'uninstalled'
    atomic_write(receipt_path, json.dumps(receipt, indent=2))
    return dict(uninstalled=True, retained='Recordings, reports and private backups')
