"""Resolve a live session by identity; never substitute the newest session."""
import os
import re
from pathlib import Path


def current_session(client="auto", session_id=None, session_file=None, home=None):
    if session_file:
        path = Path(session_file).expanduser()
        if not path.is_file():
            raise ValueError(f"Active session has no persisted transcript: {path}")
        return path
    home = home or Path.home()
    if client == "auto":
        available = [(c, os.environ.get(k)) for c, k in (("codex", "CODEX_THREAD_ID"), ("claude", "CLAUDE_SESSION_ID"))]
        available = [(c, value) for c, value in available if value]
        if len(available) != 1:
            raise ValueError("Cannot identify the active client. Pass --client and --session-id, or --session-file.")
        client, inferred = available[0]
        session_id = session_id or inferred
    session_id = session_id or os.environ.get({"codex": "CODEX_THREAD_ID", "claude": "CLAUDE_SESSION_ID"}.get(client, "CONTEXT_AUDIT_SESSION_ID"))
    if not session_id or not re.fullmatch(r"[a-zA-Z0-9_-]+", session_id):
        raise ValueError("Active session ID is missing or invalid; no other session will be selected.")
    roots = {"codex": Path(os.environ.get("CODEX_HOME", str(home / ".codex"))) / "sessions",
             "claude": home / ".claude/projects", "pi": home / ".pi/agent/sessions", "omp": home / ".omp/agent/sessions"}
    matches = [p for p in roots[client].rglob("*.jsonl") if p.stem == session_id or p.stem.endswith("-" + session_id) or p.stem.endswith("_" + session_id)]
    if len(matches) != 1:
        raise ValueError(f"Expected one transcript for active session {session_id}; found {len(matches)}. Pass --session-file explicitly.")
    return matches[0]
