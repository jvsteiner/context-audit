# Context Audit

Python CLI under `src/context_audit`, tests under `tests`. Use `uv`.

- Keep discovery, configured activation, resolved activation and observed use distinct.
- Never add installed file sizes together and label them startup token usage.
- Static scans must not execute hooks, launch MCP servers or read auth files.
- Reports omit raw secrets and source bodies; source paths remain visible.
- Client adapters must expose coverage limitations and parsing errors.
- Changes to another tool's configuration need a preview, backup and guarded restore.
- Run `uv run python -m unittest discover -s tests -v` after behavior changes.
