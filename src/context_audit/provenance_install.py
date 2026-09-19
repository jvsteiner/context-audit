"""Reversible native-observer installation; never trusts Codex hooks for the user."""
import json
import os
import shlex
import sys
import uuid
from pathlib import Path

from .installation import current, fingerprint


def status(home=None):
    root = (home or Path.home()) / '.context-audit'
    receipt = root / 'provenance-installation.json'
    data = json.loads(receipt.read_text()) if receipt.exists() else {}
    counts = {}
    for ledger in (root / 'captures').glob('*/events.jsonl'):
        for line in ledger.read_text().splitlines(keepends=True):
            if not line.endswith('\n'):
                continue
            row = json.loads(line)
            if row.get('type') == 'source-event':
                key = row['client'] + ':' + row['event']
                counts[key] = counts.get(key, 0) + 1
    return dict(installation=data.get('state', 'not-installed'), observed_event_counts=counts,
                files=[dict(path=e['path'], unchanged=fingerprint(current(Path(e['path'])) or '') == e['after_hash'])
                       for e in data.get('entries', [])],
                codex_trust='User-controlled; configuration presence does not prove hooks are trusted or active.',
                coverage='Counts prove past observations, not that every event or every session was captured.')


def prepare(home=None):
    home = home or Path.home()
    receipt_path = home / '.context-audit/provenance-installation.json'
    if receipt_path.exists() and json.loads(receipt_path.read_text())['state'] == 'installed':
        raise ValueError('Provenance observers already installed; use provenance-status or uninstall-provenance.')
    command = f"{shlex.quote(sys.executable)} -c {shlex.quote('from context_audit.cli import main; main()')} observe-event --client "
    codex_home = Path(os.environ.get('CODEX_HOME', str(home / '.codex'))) if home == Path.home() else home / '.codex'
    entries = []
    for client, path, events in (
        ('claude', home / '.claude/settings.json', ['SessionStart', 'InstructionsLoaded', 'UserPromptSubmit', 'PostToolUse', 'PostToolBatch', 'PreCompact', 'PostCompact']),
        ('codex', codex_home / 'hooks.json', ['SessionStart', 'UserPromptSubmit', 'PostToolUse', 'PreCompact', 'PostCompact'])):
        before = current(path)
        data = json.loads(before or '{}')
        hooks = data.setdefault('hooks', {})
        added = {}
        for event in events:
            group = {'matcher': '.*', 'hooks': [{'type': 'command', 'command': command + client, 'timeout': 3}]}
            if event in ('UserPromptSubmit', 'PostToolBatch'):
                group.pop('matcher')
            if group not in hooks.get(event, []):
                hooks.setdefault(event, []).append(group)
                added[event] = group
        entries.append(dict(path=str(path), before=before, after=json.dumps(data, indent=2) + '\n', added=added))
    extension = home / '.omp/agent/extensions/context-audit-observer.ts'
    body = Path(__file__).with_name('omp_observer.ts').read_text()
    before = current(extension)
    if before is not None and before != body:
        raise ValueError('Existing OMP observer differs; not overwritten.')
    if before is None:
        entries.append(dict(path=str(extension), before=None, after=body, added=None))
    return dict(entries=entries, receipt=str(receipt_path), home=str(home))


def install(apply=False, home=None):
    from .cli import atomic_write
    proposal = prepare(home)
    summary = dict(changes=[{'path': e['path'], 'events': list(e['added'] or {})} for e in proposal['entries']],
                   codex='Trust review required in Codex; installer does not approve hooks.', omp='Reload extensions or start a new OMP session.',
                   limitations='Native event observation is not complete hook-output attribution. Existing hooks are not wrapped or altered.')
    if not apply:
        return summary
    root = Path(proposal['home']) / '.context-audit/backups' / ('provenance-' + str(uuid.uuid4()))
    root.mkdir(parents=True, mode=0o700)
    for e in proposal['entries']:
        if current(Path(e['path'])) != e['before']:
            raise ValueError('Configuration changed after preparation')
    receipt = dict(state='installing', entries=[])
    for i, e in enumerate(proposal['entries']):
        backup = root / str(i)
        if e['before'] is not None:
            atomic_write(backup, e['before'])
        receipt['entries'].append({k: v for k, v in e.items() if k not in ('before', 'after')} | dict(
            backup=str(backup), existed=e['before'] is not None, before_hash=fingerprint(e['before']) if e['before'] is not None else None, after_hash=fingerprint(e['after'])))
    rp = Path(proposal['receipt'])
    atomic_write(rp, json.dumps(receipt, indent=2))
    written = []
    try:
        for e in proposal['entries']:
            path = Path(e['path'])
            if current(path) != e['before']:
                raise ValueError('Configuration changed during installation')
            path.parent.mkdir(parents=True, exist_ok=True)
            atomic_write(path, e['after'])
            written.append(e)
    except Exception:
        conflicts = []
        for e in reversed(written):
            path = Path(e['path'])
            if current(path) != e['after']:
                conflicts.append(str(path))
            elif e['before'] is None:
                path.unlink()
            else:
                atomic_write(path, e['before'])
        receipt['state'] = 'rollback-conflict' if conflicts else 'uninstalled'
        receipt['conflicts'] = conflicts
        atomic_write(rp, json.dumps(receipt, indent=2))
        raise
    receipt['state'] = 'installed'
    atomic_write(rp, json.dumps(receipt, indent=2))
    return summary | dict(installed=True, receipt=str(rp))


def uninstall(apply=False, home=None):
    from .cli import atomic_write
    root = (home or Path.home()) / '.context-audit'
    rp = root / 'provenance-installation.json'
    receipt = json.loads(rp.read_text())
    if receipt['state'] == 'uninstalled':
        return {'uninstalled': True}
    edits = []
    for e in receipt['entries']:
        path = Path(e['path'])
        before = current(path)
        if before is None:
            raise ValueError('Observer configuration missing; resolve manually')
        backup = Path(e['backup']).read_text() if e['existed'] else None
        if backup is not None and fingerprint(backup) != e['before_hash']:
            raise ValueError('Backup checksum mismatch')
        if fingerprint(before) == e['after_hash']:
            after = backup
        elif e['added'] is not None:
            data = json.loads(before)
            for event, group in e['added'].items():
                groups = data.get('hooks', {}).get(event, [])
                if groups.count(group) != 1:
                    raise ValueError('Owned observer hook changed; refusing restore')
                groups.remove(group)
                if not groups:
                    del data['hooks'][event]
            if not data.get('hooks'):
                data.pop('hooks', None)
            after = json.dumps(data, indent=2) + '\n'
        else:
            raise ValueError('Observer extension changed; refusing restore')
        edits.append((path, before, after))
    if apply:
        for path, before, after in edits:
            if current(path) != before:
                raise ValueError('Configuration changed during restore')
            if after is None:
                path.unlink()
            else:
                atomic_write(path, after)
        receipt['state'] = 'uninstalled'
        atomic_write(rp, json.dumps(receipt, indent=2))
    return dict(uninstalled=apply, paths=[str(e[0]) for e in edits], retained='Recordings and private backups')
