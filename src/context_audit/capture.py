"""Metadata-only request boundary capture. Raw payloads never enter artifacts."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path

from .core import ENCODING, tokens
from .identification import identify, text_parts
from .content_parts import parts as content_parts
from .ledger import Reader, Writer, canonical


class CaptureStore:
    def __init__(self, directory: Path, client: str):
        self.directory = directory
        self.client = client
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.key_path = directory / "fingerprint.key"
        try:
            fd = os.open(self.key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            self.key = self.key_path.read_bytes()
        else:
            self.key = secrets.token_bytes(32)
            with os.fdopen(fd, "wb") as stream:
                stream.write(self.key)
        self.lock = threading.Lock()
        self.previous = None
        self.sequence = 0
        self.sources = {}
        from collections import Counter
        reader = Reader(directory / "events.jsonl")
        for record in reader:
            if record.get("type") == "source":
                self.sources.setdefault(record["fingerprint"], []).append(record)
            elif record.get("type") in ("request", "runtime-context"):
                if record["client"] != client:
                    raise ValueError("Capture directory belongs to another client")
                self.sequence = record["sequence"]
                self.previous = Counter(x["fingerprint"] for x in record["components"])
        self.writer = Writer(reader.refs, reader.known, reader.last_id)

    def fingerprint(self, value):
        return hmac.new(self.key, canonical(value).encode(), hashlib.sha256).hexdigest()

    def append(self, *records):
        fd = os.open(self.directory / "events.jsonl", os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as stream:
            stream.write("".join(json.dumps(record, ensure_ascii=True) + "\n" for record in records))
            stream.flush()
            os.fsync(stream.fileno())

    def source(self, kind, identifier, text):
        """Register at injection time; only a keyed fingerprint and size persist."""
        row = dict(type="source", source_id=str(uuid.uuid4()), kind=kind, identifier=identifier,
                   fingerprint=self.fingerprint(text), text_tokens=tokens(text))
        with self.lock:
            self.sources.setdefault(row["fingerprint"], []).append(row)
            self.append(row)
        return row

    def request(self, payload, transport="http", session_id=None, session_evidence=None, record_type="request"):
        if record_type not in ('request', 'runtime-context'):
            raise ValueError('Invalid observation type')
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object")
        with self.lock:
            self.sequence += 1
            components = []

            def component(pointer, value, category, identity=None, role=None):
                # Names are metadata; arbitrary values, commands and headers are not.
                parts = text_parts(value)
                text = parts[0] if len(parts) == 1 else None
                matches = self.sources.get(self.fingerprint(text), []) if isinstance(text, str) else []
                components.append(dict(position=len(components), pointer=pointer, category=category,
                                       identity=identity, role=role, fingerprint=self.fingerprint(value),
                                       identification=identify(value, category, role, identity),
                                       parts=content_parts(value, category, role, self.fingerprint, pointer) if category not in ('request-parameter', 'empty-container') else [],
                                       serialized_tokens=tokens(canonical(value)), source_ids=[x["source_id"] for x in matches],
                                       attribution="exact-content-match" if len(matches) == 1 else "ambiguous-source" if matches else "request-structure-only"))

            for field, value in payload.items():
                if field in ("messages", "input") and isinstance(value, list):
                    if not value:
                        component(f"/{field}", value, "empty-container")
                    for index, item in enumerate(value):
                        role = item.get("role") if isinstance(item, dict) else None
                        category = "system-instructions" if role in ("system", "developer") else "message"
                        if isinstance(item, dict) and isinstance(item.get('tools'), list):
                            category = 'tool-definition'
                        if isinstance(item, dict) and item.get("type") in ("function_call", "custom_tool_call"):
                            category = "tool-call"
                        elif isinstance(item, dict) and item.get("type") in ("function_call_output", "custom_tool_call_output"):
                            category = "tool-result"
                        component(f"/{field}/{index}", item, category, role=role)
                elif field == "tools" and isinstance(value, list):
                    if not value:
                        component("/tools", value, "empty-container")
                    for index, item in enumerate(value):
                        name = item.get("name") or item.get("function", {}).get("name") if isinstance(item, dict) else None
                        component(f"/tools/{index}", item, "tool-definition", name)
                elif field in ("system", "instructions"):
                    if value == []:
                        component(f"/{field}", value, "empty-container")
                    for index, item in enumerate(value if isinstance(value, list) else [value]):
                        component(f"/{field}/{index}" if isinstance(value, list) else f"/{field}", item, "system-instructions")
                else:
                    component(f"/{field}", value, "request-parameter")

            from collections import Counter
            current = Counter(c["fingerprint"] for c in components)
            old = Counter(self.previous or {})
            delta = dict(added=sum((current - old).values()), removed=sum((old - current).values()),
                         retained=sum((old & current).values()))
            reference = bool(payload.get("previous_response_id"))
            row = dict(type=record_type, schema_version=3, request_id=str(uuid.uuid4()), sequence=self.sequence,
                       session_id=session_id, session_evidence=session_evidence,
                       timestamp=datetime.now(timezone.utc).isoformat(), client=self.client, transport=transport,
                       tokenizer=ENCODING, serialized_request_tokens=tokens(canonical(payload)),
                       request_fingerprint=self.fingerprint(payload), components=components, delta=delta,
                       capture_status="complete-json-body", context_status="server-reference-unresolved" if reference else "client-body-observed",
                       attribution_status="partial", coverage_scope="Only requests received by this capture endpoint; not proof all client traffic was intercepted.",
                       limitations=["Serialized token estimates are not provider token counts or additive component accounting.",
                                    "Message bodies may combine several sources; source identity requires injection-side instrumentation.",
                                    "Media, encrypted content and server-side context are fingerprinted but not semantically reconstructed."])
            if record_type == 'runtime-context':
                for item in components:
                    if item['attribution'] == 'request-structure-only':
                        item['attribution'] = 'runtime-structure-only'
                row.update(capture_status='runtime-snapshot', context_status='not-a-provider-request',
                           coverage_scope='Effective system prompt and enabled tool schemas at the startup observer callback; later extensions and provider serialization may change them.')
            # Unchanged component bodies and the unchanged prefix are not written again.
            self.append(*self.writer.encode(row))
            self.previous = current
            return row
