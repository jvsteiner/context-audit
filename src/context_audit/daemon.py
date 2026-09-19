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
from .recordings import valid_id


class SessionStores:
    def __init__(self, root, client):
        self.root, self.client = root, client
        self.stores = {}
        self.lock = threading.Lock()

    def for_session(self, session_id):
        key = session_id if valid_id(session_id) else 'unidentified-' + str(uuid.uuid4())
        with self.lock:
            if key not in self.stores:
                self.stores[key] = CaptureStore(self.root / f'{self.client}-{key}', self.client)
            return self.stores[key]


def serve(config_path):
    config = json.loads(config_path.read_text())
    fd = os.open(config_path.with_suffix('.lock'), os.O_CREAT | os.O_RDWR, 0o600)
    gateways = []
    stopped = threading.Event()
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        for client, route in config['routes'].items():
            gateway = Gateway(route['upstream'], SessionStores(Path(config['captures']), client),
                              port=route['port'], prefix=route['prefix'])
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
