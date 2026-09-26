"""Compact capture ledgers and bounded capture storage.

A component body (metadata only) is written once as a `component` row keyed by
its hash. Each request row carries `component_refs`: keep the first `keep`
(pointer, hash) pairs of the previous request, then append `add`. `base` names
the previous request so a broken chain fails loudly instead of misreporting.
Rows with an inline `components` list use the earlier full format and stay readable.
"""
import hashlib
import json
import os
import shutil
import time
from pathlib import Path

LIMIT_BYTES = 2 * 1024 ** 3
ACTIVE_SECONDS = 3600


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def body(component):
    return {k: v for k, v in component.items() if k not in ('position', 'pointer')}


def body_hash(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()[:32]


class Reader:
    """Streams one ledger. After iteration, `refs`, `known` and `last_id` describe its tail."""

    def __init__(self, path, expand=True):
        self.path, self.expand = Path(path), expand
        self.bodies, self.refs, self.last_id = {}, None, None

    @property
    def known(self):
        return set(self.bodies)

    def __iter__(self):
        if not self.path.exists():
            return
        with self.path.open() as stream:
            for line in stream:
                # A live append can leave a final line unfinished. Never ignore corrupt complete lines.
                if not line.endswith('\n'):
                    continue
                row = json.loads(line)
                kind = row.get('type')
                if kind == 'component':
                    self.bodies[row['hash']] = row['body']
                    continue
                if kind in ('request', 'runtime-context'):
                    if 'component_refs' in row:
                        edit = row.pop('component_refs')
                        if edit['base'] != self.last_id or (edit['keep'] and self.refs is None) or edit['keep'] > len(self.refs or ()):
                            raise ValueError(f'Broken request chain in {self.path} at sequence {row.get("sequence")}')
                        self.refs = (self.refs or [])[:edit['keep']] + [tuple(x) for x in edit['add']]
                        if self.expand:
                            row['components'] = [dict(position=i, pointer=p, **self.bodies[h]) for i, (p, h) in enumerate(self.refs)]
                    else:
                        self.refs = None
                    self.last_id = row['request_id']
                yield row


def records(directory, expand=True):
    return list(Reader(Path(directory) / 'events.jsonl', expand))


class Writer:
    """Encodes expanded request rows as component rows plus a prefix edit."""

    def __init__(self, refs=None, known=(), last_id=None):
        self.refs, self.known, self.last_id = refs, set(known), last_id

    def encode(self, row):
        refs, lines = [], []
        for component in row['components']:
            content = body(component)
            digest = body_hash(content)
            refs.append((component['pointer'], digest))
            if digest not in self.known:
                self.known.add(digest)
                lines.append(dict(type='component', hash=digest, body=content))
        keep = 0
        while self.refs is not None and keep < min(len(self.refs), len(refs)) and self.refs[keep] == refs[keep]:
            keep += 1
        compact = {k: v for k, v in row.items() if k != 'components'}
        compact['component_refs'] = dict(base=self.last_id, keep=keep, add=[list(x) for x in refs[keep:]])
        lines.append(compact)
        self.refs, self.last_id = refs, row['request_id']
        return lines


def compact(directory, idle_seconds=900):
    """Rewrite a full-format ledger compactly, verify it row by row, then swap it in."""
    path = Path(directory) / 'events.jsonl'
    if not path.is_file():
        return dict(directory=str(directory), status='no-ledger')
    if time.time() - path.stat().st_mtime < idle_seconds:
        return dict(directory=str(directory), status='skipped-active')
    before = path.stat().st_size
    with path.open() as stream:
        if not any('"components":' in line for line in stream):
            return dict(directory=str(directory), status='already-compact', bytes=before)
    temporary = path.with_name('events.jsonl.compact')
    writer, reader = Writer(), Reader(path)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, 'w') as out:
            for row in reader:
                if row.get('type') in ('request', 'runtime-context'):
                    for line in writer.encode(row):
                        out.write(json.dumps(line, ensure_ascii=True) + '\n')
                else:
                    out.write(json.dumps(row, ensure_ascii=True) + '\n')
            out.flush()
            os.fsync(out.fileno())
        old, new = iter(Reader(path)), iter(Reader(temporary))
        for expected in old:
            if next(new, None) != expected:
                raise ValueError(f'Compacted ledger does not match the original: {path}')
        if next(new, None) is not None:
            raise ValueError(f'Compacted ledger has extra rows: {path}')
        if path.stat().st_size != before:
            raise ValueError(f'Ledger changed during compaction: {path}')
        # Keep the original age so pruning still treats the recording as old.
        stat = path.stat()
        os.utime(temporary, ns=(stat.st_atime_ns, stat.st_mtime_ns))
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()
    return dict(directory=str(directory), status='compacted', bytes_before=before, bytes_after=path.stat().st_size)


def usage(root):
    rows = []
    for directory in Path(root).iterdir() if Path(root).is_dir() else ():
        if not directory.is_dir() or directory.is_symlink():
            continue
        files = [f for f in directory.iterdir() if f.is_file()]
        rows.append(dict(directory=directory, bytes=sum(f.stat().st_size for f in files),
                         modified=max((f.stat().st_mtime for f in files), default=directory.stat().st_mtime)))
    return sorted(rows, key=lambda r: r['modified'])


def prune(root, limit=LIMIT_BYTES, protect=(), apply=False, active_seconds=ACTIVE_SECONDS):
    """Delete whole recordings, oldest first, until captures fit the limit.

    Recordings written within `active_seconds`, and protected ones, are kept even over the limit.
    """
    rows = usage(root)
    total = sum(r['bytes'] for r in rows)
    protected = {Path(p).resolve() for p in protect}
    now, removed = time.time(), []
    for row in rows:
        if total <= limit:
            break
        if row['directory'].resolve() in protected or now - row['modified'] < active_seconds:
            continue
        if apply:
            shutil.rmtree(row['directory'])
        removed.append(dict(directory=str(row['directory']), bytes=row['bytes']))
        total -= row['bytes']
    return dict(limit_bytes=limit, bytes_after=total, applied=apply, removed=removed)
