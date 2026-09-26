"""Persistent local recorder with separate ledgers for exact client sessions."""
import fcntl
import json
import os
import signal
import threading
import uuid
from pathlib import Path

from .capture import CaptureStore
from .gateway import Gateway
from .ledger import LIMIT_BYTES, prune
from .recordings import valid_id


PRUNE_LOCK = threading.Lock()


class SessionStores:
    def __init__(self, root, client, limit=LIMIT_BYTES):
        self.root, self.client, self.limit = root, client, limit
        self.stores = {}
        self.lock = threading.Lock()

    def for_session(self, session_id):
        key = session_id if valid_id(session_id) else 'unidentified-' + str(uuid.uuid4())
        with self.lock:
            store = self.stores.get(key)
            # A pruned recording starts again as a new ledger.
            if store is None or not store.directory.is_dir():
                directory = self.root / f'{self.client}-{key}'
                with PRUNE_LOCK:
                    try:
                        prune(self.root, self.limit, protect=[directory], apply=True)
                    except OSError:
                        pass  # Cleanup must never stop recording; the CLI prune reports errors.
                self.stores[key] = CaptureStore(directory, self.client)
            return self.stores[key]


def serve(config_path):
    config = json.loads(config_path.read_text())
    fd = os.open(config_path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR, 0o600)
    gateways = []
    stopped = threading.Event()
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        event_stores = {client: SessionStores(Path(config['captures']), client, config.get('capture_limit_bytes', LIMIT_BYTES)) for client in ('codex', 'claude', 'omp', 'pi')}
        for client, route in config['routes'].items():
            gateway = Gateway(route['upstream'], event_stores[client],
                              port=route['port'], prefix=route['prefix'], event_stores=event_stores)
            gateway.start()
            gateways.append(gateway)
        def stop(*_):
            stopped.set()
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        stopped.wait()
    finally:
        for gateway in gateways:
            gateway.close()
        os.close(fd)
