from __future__ import annotations

import argparse
import difflib
import json
import os
import sys
import tempfile
from pathlib import Path

from .core import audit_tools, compare, digest, document, observe, read, scan
from .sessions import analyze, discover


def atomic_write(path: Path, text: str):
    mode = path.stat().st_mode & 0o777 if path.exists() else 0o600
    fd, temporary = tempfile.mkstemp(prefix=".context-audit-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def disable_hook(path: Path, event: str, group: int, hook: int, apply: bool) -> dict:
    path = path.resolve()
    before = read(path)
    data = json.loads(before)
    if group < 0 or hook < 0:
        raise ValueError("Hook indexes must be nonnegative")
    hooks = data["hooks"][event][group]["hooks"]
    hooks.pop(hook)
    if not hooks:
        data["hooks"][event].pop(group)
    after = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    result = {"applied": False, "path": str(path), "diff": "".join(difflib.unified_diff(
        before.splitlines(True), after.splitlines(True), fromfile=str(path), tofile=str(path)))}
    if apply:
        backup_dir = path.parent / ".context-audit"
        backup_dir.mkdir(mode=0o700, exist_ok=True)
        backup = backup_dir / f"{path.name}.{digest(before)[:16]}.backup"
        if not backup.exists():
            atomic_write(backup, before)
            os.chmod(backup, 0o600)
        receipt = backup.with_suffix(".receipt.json")
        atomic_write(receipt, json.dumps({"path": str(path), "backup": str(backup),
                                         "before_sha256": digest(before), "after_sha256": digest(after)}, indent=2))
        if read(path) != before:
            raise ValueError("Configuration changed while planning; refusing to overwrite")
        atomic_write(path, after)
        result.update(applied=True, receipt=str(receipt))
    return result


def restore(receipt: Path) -> dict:
    data = document(receipt)
    path, backup = Path(data["path"]), Path(data["backup"])
    if digest(read(path)) != data["after_sha256"]:
        raise ValueError("Configuration changed since this edit; restore would overwrite newer changes")
    original = read(backup)
    if digest(original) != data["before_sha256"]:
        raise ValueError("Backup checksum does not match receipt")
    atomic_write(path, original)
    return {"restored": str(path)}


def markdown(report: dict) -> str:
    lines = ["# Context audit", "", f"Project: `{report['project']}`", "", report["measurement"], "",
             "## Estimated text tokens by loading category", "", "| Category | Tokens |", "|---|---:|"]
    for category, count in sorted(report["totals_by_loading"].items()):
        lines.append(f"| {category} | {count:,} |")
    lines.extend(["", "## Inventory", "", "| Kind | Loading | Tokens | Source |", "|---|---|---:|---|"])
    for item in sorted(report["items"], key=lambda x: -x["tokens"]):
        path = item["path"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {item['kind']} | {item['loading']} | {item['tokens']:,} | {path} |")
    lines.extend(["", "## Findings", ""])
    for item in report["findings"]:
        lines.append(f"- **{item['code']}** — {item['message']} (`{item['source']}`)")
    lines.extend(["", "## Coverage limits", "", *[f"- {x}" for x in report["limitations"]]])
    if report["errors"]:
        lines.extend(["", "## Read errors", "", *[f"- {x['path']}: {x['error']}" for x in report["errors"]]])
    return "\n".join(lines) + "\n"


def session_markdown(report: dict) -> str:
    lines = ["# Historical context audit", "", f"Client: {report['client']} · Session: `{report['path']}`", "",
             f"Skill text attributed from recorded content: **{report['attributed_skill_returned_tokens']:,} estimated tokens**.",
             f"Recorded requests: {report['request_count']} · Compactions: {len(report['compactions'])} · "
             f"Outputs with unknown skill attribution: {report['ambiguous_skill_outputs']}", "",
             "This measures text introduced by skill reads/injections, not retained context or cumulative billing.", "",
             "| Skill source | Read/injection events | Attributed tokens | Unknown/failed events |", "|---|---:|---:|---:|"]
    for item in report["skills"]:
        source = item["source"].replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {source} | {item['reads']} | {item['attributed_returned_tokens']:,} | {item['ambiguous_reads']} |")
    if report["context_snapshots"]:
        latest = report["context_snapshots"][-1]
        lines.extend(["", "Latest recorded context snapshot:", "", "```json", json.dumps(latest, indent=2), "```"])
    lines.extend(["", "Reported request usage (provider fields, not additive categories):", "", "```json",
                  json.dumps(report["reported_request_usage"], indent=2), "```", "", "Coverage limits:", "",
                  *[f"- {x}" for x in report["limitations"]]])
    if report["errors"]:
        lines.extend(["", f"Parse errors: {len(report['errors'])}; use --format json for details."])
    return "\n".join(lines) + "\n"


def main():
    parser = argparse.ArgumentParser(description="Inventory, measure, and track coding-agent context sources.")
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ('install-provenance', 'uninstall-provenance'):
        p = sub.add_parser(name, help='Preview native lifecycle observer changes; --apply makes them')
        p.add_argument('--apply', action='store_true')
    sub.add_parser('provenance-status', help='Show installed observers and observed native events')
    p = sub.add_parser('observe-event', help=argparse.SUPPRESS)
    p.add_argument('--client', choices=('claude', 'codex'), required=True)
    p = sub.add_parser('install', help='Preview default-on macOS recording; --apply installs and starts it')
    p.add_argument('--codex-auth', choices=('chatgpt', 'api-key'), help='Defaults to the method reported by codex login status')
    p.add_argument('--apply', action='store_true')
    p = sub.add_parser('uninstall', help='Preview restoring default routing; --apply restores and stops recording')
    p.add_argument('--apply', action='store_true')
    sub.add_parser('status', help='Check persistent recorder health and installed configuration')
    p = sub.add_parser('service', help=argparse.SUPPRESS)
    p.add_argument('--config', type=Path, required=True)
    p = sub.add_parser('run', help='Launch a client with experimental per-process HTTP recording')
    p.add_argument('--client', choices=('codex', 'claude'), required=True)
    p.add_argument('--upstream', required=True, help='Explicit provider base URL; credentials are forwarded in memory')
    p.add_argument('--auth', choices=('chatgpt', 'api-key'), help='Required for Codex')
    p.add_argument('client_args', nargs=argparse.REMAINDER)
    p = sub.add_parser('captures', help='List metadata recordings and their observed session identities')
    p = sub.add_parser("probe-capture", help="Test installed clients against a local metadata-only rejecting endpoint")
    p.add_argument("--client", choices=("codex", "claude", "all"), default="all")
    p.add_argument("--output-dir", type=Path, required=True)
    p = sub.add_parser("capture-report", help="Visualize one metadata-captured outbound request")
    p.add_argument("directory", type=Path)
    p.add_argument("--sequence", type=int)
    p.add_argument('--session-id')
    p.add_argument("--output", type=Path, required=True)
    p = sub.add_parser("capture-serve", help="Run an explicit opt-in HTTP metadata capture gateway")
    p.add_argument("--client", choices=("codex", "claude"), required=True)
    p.add_argument("--upstream", required=True)
    p.add_argument("--output-dir", type=Path, required=True)
    p = sub.add_parser("current", help="Open the exact live session using client-provided identity")
    p.add_argument("--client", choices=("auto", "codex", "claude", "pi", "omp"), default="auto")
    p.add_argument("--session-id")
    p.add_argument("--session-file", type=Path)
    p.add_argument("--output", type=Path)
    p.add_argument("--no-open", action="store_true")
    p = sub.add_parser("install-integrations", help="Install user commands for Claude, Codex, Pi and OMP")
    p.add_argument("--project", type=Path, help="Context Audit checkout location")
    p = sub.add_parser("scan", help="Read local setup; does not run hooks or MCP servers")
    p.add_argument("project", nargs="?", type=Path, default=Path.cwd())
    p.add_argument("--home", type=Path, default=Path.home())
    p.add_argument("--card", type=Path, action="append", default=[])
    p.add_argument("--format", choices=("json", "markdown"), default="markdown")
    p.add_argument("--output", type=Path)
    p = sub.add_parser("visualize", help="Generate an offline interactive session provenance timeline")
    p.add_argument("path", type=Path, nargs="?", help="Session JSONL file, or project directory to filter the picker")
    p.add_argument("--client", choices=("auto", "codex", "claude", "pi", "omp"), default="auto")
    p.add_argument("--output", type=Path, help="HTML destination (default: ~/.context-audit/reports/SESSION.html)")
    p.add_argument("--latest", action="store_true", help="Use the newest matching session without a picker")
    p.add_argument("--no-open", action="store_true", help="Generate HTML without opening a browser")
    p = sub.add_parser("diff", help="Compare two JSON scan snapshots")
    p.add_argument("before", type=Path)
    p.add_argument("after", type=Path)
    p = sub.add_parser("tools", help="Audit exported MCP tools/list JSON")
    p.add_argument("schema", type=Path)
    p = sub.add_parser("observe", help="Read usage from explicit Codex/Claude JSONL session files")
    p.add_argument("sessions", nargs="+", type=Path)
    p = sub.add_parser("sessions", help="List local Codex, Claude, Pi and OMP session logs")
    p.add_argument("--home", type=Path, default=Path.home())
    p.add_argument("--client", choices=("all", "codex", "claude", "pi", "omp"), default="all")
    p.add_argument("--limit", type=int, default=30)
    p.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of a session list")
    p = sub.add_parser("session", help="Audit historical skill reads, hook context, usage and compactions")
    p.add_argument("path", type=Path)
    p.add_argument("--client", choices=("auto", "codex", "claude", "pi", "omp"), default="auto")
    p.add_argument("--format", choices=("json", "markdown"), default="markdown")
    p.add_argument("--output", type=Path)
    p = sub.add_parser("disable-hook", help="Preview a JSON hook removal; --apply creates a backup and receipt")
    p.add_argument("config", type=Path)
    p.add_argument("event")
    p.add_argument("group", type=int)
    p.add_argument("hook", type=int)
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("restore", help="Restore a prior edit if the target and backup checksums match")
    p.add_argument("receipt", type=Path)
    args = parser.parse_args()
    try:
        if args.command == 'observe-event':
            from .observer import observe_stdin
            observe_stdin(args.client)
            return
        if args.command in ('install-provenance', 'uninstall-provenance'):
            from .provenance_install import install, uninstall
            operation = install if args.command == 'install-provenance' else uninstall
            print(json.dumps(operation(), indent=2), flush=True)
            if args.apply:
                print(json.dumps(operation(apply=True), indent=2))
            return
        if args.command == 'provenance-status':
            from .provenance_install import status
            print(json.dumps(status(), indent=2))
            return
        if args.command == 'install':
            from .installation import plan, preview, apply
            proposal = plan(codex_auth=args.codex_auth)
            print(json.dumps(preview(proposal), indent=2), flush=True)
            if args.apply:
                print(json.dumps(apply(proposal), indent=2))
            else:
                print('Preview only. Add --apply to install default recording and any missing client commands.')
            return
        if args.command == 'uninstall':
            from .installation import uninstall
            print(json.dumps(uninstall(apply_changes=args.apply), indent=2))
            return
        if args.command == 'status':
            from .installation import status
            print(json.dumps(status(), indent=2))
            return
        if args.command == 'service':
            from .daemon import serve
            serve(args.config)
            return
        if args.command == 'run':
            from .launch import run
            rest = args.client_args[1:] if args.client_args[:1] == ['--'] else args.client_args
            sys.exit(run(args.client, args.upstream, args.auth, rest))
        if args.command == 'captures':
            from .recordings import discover
            rows = discover()
            for row in rows:
                print(f"{row['client']} · {row['session_id'] or 'unidentified session'} · {row['requests']} requests · {row['directory']}")
            if not rows:
                print('No metadata recordings found. Installation alone does not enable recording.')
            return
        if args.command == "capture-report":
            from .recording_view import render_recording
            atomic_write(args.output, render_recording(args.directory, args.session_id, args.sequence))
            print(f"Wrote {args.output.resolve()}")
            return
        if args.command == "capture-serve":
            import time
            from .capture import CaptureStore
            from .gateway import Gateway
            gateway = Gateway(args.upstream, CaptureStore(args.output_dir, args.client))
            gateway.start()
            print(f"Capture endpoint: {gateway.url}", flush=True)
            print("HTTP POST only. Configure this endpoint for one client session. Ctrl-C stops capture.", flush=True)
            try:
                while True:
                    time.sleep(1)
            finally:
                gateway.close()
            return
        if args.command == "probe-capture":
            from .probe import probe
            clients = ("codex", "claude") if args.client == "all" else (args.client,)
            print(json.dumps([probe(c, args.output_dir / c) for c in clients], indent=2))
            return
        if args.command == "install-integrations":
            from .integrations import install
            print("Installed:\n" + "\n".join(install(project=args.project)))
            return
        if args.command == "current":
            from .recordings import resolve
            from .recording_view import render_recording
            import webbrowser
            client = args.client
            if client == 'auto':
                clients = [c for c, key in (('codex', 'CODEX_THREAD_ID'), ('claude', 'CLAUDE_SESSION_ID')) if os.environ.get(key)]
                if len(clients) != 1:
                    raise ValueError('Cannot identify the active client; pass --client and --session-id.')
                client = clients[0]
            session_id = args.session_id or os.environ.get({'codex': 'CODEX_THREAD_ID', 'claude': 'CLAUDE_SESSION_ID'}.get(client, 'CONTEXT_AUDIT_SESSION_ID'))
            if client in ('pi', 'omp') and not session_id and args.session_file:
                with args.session_file.open() as stream:
                    header = json.loads(stream.readline())
                if header.get('type') != 'session':
                    raise ValueError('Expected a native session header; pass --session-id explicitly.')
                session_id = header.get('id')
            directory = resolve(client, session_id)
            output = args.output or Path.home() / '.context-audit/reports' / f'{client}-{session_id}-capture.html'
            output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            atomic_write(output, render_recording(directory, session_id))
            print(f'Wrote {output.resolve()} (entire observed session; see provenance evidence and coverage in the report)')
            if not args.no_open:
                webbrowser.open(output.resolve().as_uri())
            return
        if args.command == "scan":
            if not args.project.is_dir() or not args.home.is_dir():
                raise ValueError("Project and home must be existing directories")
            result = scan(args.project, args.home, args.card)
            output = markdown(result) if args.format == "markdown" else json.dumps(result, indent=2)
            if args.output:
                atomic_write(args.output, output)
            else:
                print(output)
            return
        if args.command == "visualize":
            from .visualize import render
            from .picker import candidates, choose
            import webbrowser
            path = args.path
            if path is None or path.is_dir():
                path = choose(candidates(Path.home(), args.client, path), args.latest, sys.stdin.isatty())
                if path is None:
                    return
            if not path.is_file():
                raise ValueError(f"Session file does not exist: {path}. Run visualize without a path to choose a session.")
            output = args.output
            if output is None:
                folder = Path.home() / ".context-audit/reports"
                folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                output = folder / f"{path.stem}-{digest(str(path.resolve()))[:8]}.html"
            elif output.is_dir():
                output = output / f"{path.stem}.html"
            result = analyze(path, args.client)
            atomic_write(output, render(result))
            print(f"Wrote {output.resolve()} ({len(result['timeline']['blocks'])} context blocks)")
            if not args.no_open and not webbrowser.open(output.resolve().as_uri()):
                print("Could not open a browser automatically. Open the HTML file above.")
            return
        if args.command == "diff":
            result = compare(document(args.before), document(args.after))
        elif args.command == "tools":
            result = audit_tools(document(args.schema))
        elif args.command == "observe":
            result = observe(args.sessions)
        elif args.command == "sessions":
            from .picker import candidates, table
            if args.limit < 1:
                raise ValueError("--limit must be at least 1")
            result = candidates(args.home, args.client, limit=args.limit)
            if not args.json:
                print(table(result) if result else "No sessions found.")
                print("\nRun context-audit visualize to choose and open a session.")
                return
        elif args.command == "session":
            result = analyze(args.path, args.client)
            output = session_markdown(result) if args.format == "markdown" else json.dumps(result, indent=2)
            if args.output:
                atomic_write(args.output, output)
            else:
                print(output)
            return
        elif args.command == "disable-hook":
            result = disable_hook(args.config, args.event, args.group, args.hook, args.apply)
        else:
            result = restore(args.receipt)
        print(json.dumps(result, indent=2))
    except (EOFError, KeyboardInterrupt):
        parser.exit(0, "\nCancelled.\n")
    except (OSError, ValueError, KeyError, IndexError, TypeError) as exc:
        parser.exit(2, f"context-audit: {exc}\n")


if __name__ == "__main__":
    main()
