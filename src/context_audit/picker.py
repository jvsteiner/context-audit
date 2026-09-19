"""Human-readable session discovery and terminal selection."""
import json
from datetime import datetime
from itertools import islice
from pathlib import Path

from .sessions import discover


def candidates(home, client="auto", directory=None, limit=30):
    rows = discover(home, "all" if client == "auto" else client, 100000 if directory else limit)
    found = []
    for row in rows:
        cwd = None
        try:
            with Path(row["path"]).open(encoding="utf-8") as stream:
                for line in islice(stream, 40):
                    try:
                        event = json.loads(line)
                    except ValueError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    payload = event.get("payload") or {}
                    cwd = event.get("cwd") or (payload.get("cwd") if isinstance(payload, dict) else None)
                    if isinstance(cwd, str):
                        break
                    cwd = None
        except (OSError, UnicodeError):
            continue
        if directory and (not cwd or not Path(cwd).resolve().is_relative_to(directory.resolve())):
            continue
        found.append(dict(row, cwd=cwd))
        if len(found) >= limit:
            break
    return found


def table(rows):
    lines = [" #  Client   Modified          Project", ""]
    for i, row in enumerate(rows, 1):
        date = datetime.fromtimestamp(row["modified"]).strftime("%Y-%m-%d %H:%M")
        lines.append(f"{i:2}  {row['client']:<7}  {date}  {row.get('cwd') or '(project not recorded)'}")
        lines.append(f"    {row['path']}")
    return "\n".join(lines)


def choose(rows, latest=False, interactive=True):
    if not rows:
        raise ValueError("No matching sessions found. Try visualize without a directory or --client filter.")
    if latest:
        return Path(rows[0]["path"])
    if not interactive:
        raise ValueError("Session selection needs a terminal. Use --latest or provide a session JSONL path.")
    print(table(rows))
    while True:
        answer = input("\nSession number [1 = most recent, q = cancel]: ").strip()
        if answer.lower() in ("q", "quit"):
            return None
        if not answer:
            answer = "1"
        if answer.isdigit() and 1 <= int(answer) <= len(rows):
            return Path(rows[int(answer) - 1]["path"])
        print(f"Enter a number from 1 to {len(rows)}, or q to cancel.")
