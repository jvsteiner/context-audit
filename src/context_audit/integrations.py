"""Install small client entrypoints without replacing existing integrations."""
import json
import shlex
from pathlib import Path


def integration_files(home=None, project=None):
    home = home or Path.home()
    project = (project or Path(__file__).resolve().parents[2]).resolve()
    if not (project / "pyproject.toml").is_file():
        raise ValueError("Run install-integrations from an editable checkout, or pass --project.")
    command = f"uv run --project {shlex.quote(str(project))} context-audit current"
    claude = '''---
name: context-audit
description: Open the context provenance timeline for this active session.
---

Run the following command using Bash to generate and open the current session's context map:

```sh
COMMAND --client claude --session-id '${CLAUDE_SESSION_ID}'
```

Report the generated HTML path. This is a snapshot of the transcript persisted so far.
If session identity is unavailable, report the error; do not select a recent session instead.
'''.replace("COMMAND", command)
    codex = '''---
name: context-audit
description: Open the context provenance timeline for this active Codex session.
---

Run this shell command to generate and open the current session's context map:

```sh
COMMAND --client codex
```

It resolves the exact session using CODEX_THREAD_ID. Report the generated HTML path.
If the variable or persisted transcript is unavailable, report the error. Do not use --latest.
The report is a snapshot, not a continuously updating view.
'''.replace("COMMAND", command)
    extension = '''// Context Audit: user-invoked command, no model turn or startup injection.
import { execFile } from "node:child_process";
import { promisify } from "node:util";
const exec = promisify(execFile);
export default function (pi: any) {
  pi.registerCommand("context-audit", {
    description: "Open a context timeline for the current session",
    handler: async (_args: string, ctx: any) => {
      const sessionId = ctx.sessionManager.getSessionId();
      if (!sessionId) { ctx.ui.notify("Active session identity is unavailable.", "warning"); return; }
      ctx.ui.notify("Generating context map…", "info");
      try {
        const { stdout } = await exec("uv", ["run", "--project", PROJECT,
          "context-audit", "current", "--client", CLIENT, "--session-id", sessionId],
          { timeout: 120000, maxBuffer: 1024 * 1024 });
        ctx.ui.notify(stdout.trim(), "info");
      } catch (error: any) { ctx.ui.notify(error.stderr || error.message, "error"); }
    }
  });
}
'''.replace("PROJECT", json.dumps(str(project)))
    codex_home = home / ".codex"
    entries = {home / ".claude/skills/context-audit/SKILL.md": claude,
               codex_home / "skills/context-audit/SKILL.md": codex,
               home / ".pi/agent/extensions/context-audit.ts": extension.replace("CLIENT", '"pi"'),
               home / ".omp/agent/extensions/context-audit.ts": extension.replace("CLIENT", '"omp"')}
    return entries


def install(home=None, project=None):
    from .cli import atomic_write
    entries = integration_files(home, project)
    # Preflight all destinations before writing any file.
    for path, text in entries.items():
        if path.exists() and path.read_text() != text:
            raise ValueError(f"Existing integration differs; refusing to overwrite: {path}")
    for path, text in entries.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            atomic_write(path, text)
    return [str(p) for p in entries]
