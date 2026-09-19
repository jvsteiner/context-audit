from __future__ import annotations

import hashlib
import json
import os
import re
import tomllib
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import tiktoken
import yaml

ENCODING = "o200k_base"
MAX_BYTES = 2_000_000


def tokens(text: str) -> int:
    return len(tiktoken.get_encoding(ENCODING).encode(text, disallowed_special=()))


def digest(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def read(path: Path) -> str:
    if path.stat().st_size > MAX_BYTES:
        raise ValueError(f"File exceeds {MAX_BYTES} bytes: {path}")
    return path.read_text(encoding="utf-8")


def document(path: Path) -> dict:
    text = read(path)
    if path.suffix == ".toml":
        value = tomllib.loads(text)
    elif path.suffix in (".yml", ".yaml"):
        value = yaml.safe_load(text)
    else:
        value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Expected an object")
    return value


def frontmatter(text: str) -> tuple[dict, str]:
    match = re.match(r"\A---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if not match:
        return {}, text
    metadata = yaml.safe_load(match[1]) or {}
    return (metadata if isinstance(metadata, dict) else {}), text[match.end():]


def scan(project: Path, home: Path, extra: list[Path] | None = None) -> dict:
    project, home = project.resolve(), home.resolve()
    codex = Path(os.environ.get("CODEX_HOME", str(home / ".codex"))) if home == Path.home() else home / ".codex"
    items, findings, errors = [], [], []
    seen = set()

    def finding(code, source, message):
        findings.append(dict(code=code, source=source, message=message))

    def add(path, kind, scope, loading="conditional", text=None, key="", details=None):
        identity = f"{path}:{kind}:{key}"
        if identity in seen:
            return
        seen.add(identity)
        try:
            body = read(path) if text is None else text
            entry = dict(id=identity, path=str(path), kind=kind, scope=scope,
                         loading=loading, tokens=tokens(body), bytes=len(body.encode()),
                         sha256=digest(body), details=details or {})
            items.append(entry)
            if kind in ("instructions", "rule", "skill-body", "agent", "context-card"):
                if entry["tokens"] > 2000:
                    finding("large-document", identity, f"{entry['tokens']} estimated tokens; consider narrower loading scope.")
                lines = [line.strip() for line in body.splitlines() if len(line.strip()) > 60]
                repeats = sum(n - 1 for n in Counter(lines).values() if n > 1)
                if repeats:
                    finding("repeated-instructions", identity, f"{repeats} repeated long lines.")
                if re.search(r"\b(always|whenever|every session)\b", body, re.I):
                    entry["details"]["broad_trigger_language"] = True
                imports = re.findall(r"(?:^|\s)@([\w./~\-]+)", body)
                if imports:
                    entry["details"]["import_candidates"] = imports
                    finding("unresolved-imports", identity, "Contains @ references; imported content is not included in this estimate.")
            return entry
        except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
            errors.append(dict(path=str(path), error=str(exc)))

    def files(base, pattern, kind, scope, loading="conditional"):
        if base.exists():
            for path in sorted(base.glob(pattern)):
                if path.is_file():
                    add(path, kind, scope, loading)

    def skills(base, scope, loading="discovered"):
        if not base.exists():
            return
        # Explicit depth also finds symlinked skill directories without recursively
        # following arbitrary filesystem links.
        for path in sorted(base.glob("*/SKILL.md")):
            try:
                text = read(path)
                meta, _ = frontmatter(text)
                summary = json.dumps({k: meta.get(k, "") for k in ("name", "description")}, ensure_ascii=False)
                add(path, "skill-metadata", scope, loading, text=summary)
                add(path, "skill-body", scope, "on-demand")
                if not meta.get("description"):
                    finding("missing-skill-description", str(path), "Skill has no discovery description.")
            except (OSError, UnicodeError, ValueError, yaml.YAMLError) as exc:
                errors.append(dict(path=str(path), error=str(exc)))

    def config(path, scope, owner):
        if not path.is_file():
            return
        try:
            data = document(path)
        except (OSError, UnicodeError, ValueError) as exc:
            errors.append(dict(path=str(path), error=str(exc)))
            return
        # Never retain environment values, URLs, credentials, or raw config.
        add(path, "configuration", scope, "not-prompt", text=json.dumps(sorted(data)),
            details={"owner": owner, "measurement": "top-level keys only; not config contents"})
        containers = [(data, "")]
        project_data = data.get("projects", {}).get(str(project), {})
        if isinstance(project_data, dict):
            containers.append((project_data, f"projects/{project}/"))
        for container, prefix in containers:
            servers = container.get("mcp_servers", container.get("mcpServers", {}))
            if isinstance(servers, dict):
                for name, server in servers.items():
                    if not isinstance(server, dict):
                        continue
                    details = {"name": name, "owner": owner, "transport": "stdio" if "command" in server else "remote",
                               "enabled": server.get("enabled", True), "schema_tokens": None,
                               "environment_keys": sorted(server.get("env", {})) if isinstance(server.get("env", {}), dict) else []}
                    add(path, "mcp-server", scope, "configured", text="", key=prefix + name, details=details)
                    finding("unmeasured-mcp", f"{path}:{name}", "Configured server; import tools/list JSON with the tools command to measure its schema footprint.")
        hooks = data.get("hooks", {})
        if isinstance(hooks, dict):
            for event, groups in hooks.items():
                if not isinstance(groups, list):
                    continue
                for gi, group in enumerate(groups):
                    if not isinstance(group, dict):
                        continue
                    for hi, hook in enumerate(group.get("hooks", [])):
                        if not isinstance(hook, dict):
                            continue
                        payload = str(hook.get("command", hook.get("prompt", hook.get("url", ""))))
                        key = f"{event}/{gi}/{hi}"
                        details = {"event": event, "group_index": gi, "hook_index": hi,
                                   "type": hook.get("type", "command"), "matcher": group.get("matcher", ""),
                                   "timeout": hook.get("timeout"), "payload_sha256": digest(payload),
                                   "runtime_output_tokens": None,
                                   "measurement": "configuration payload only, not runtime context"}
                        add(path, "hook", scope, "event-driven", text=payload, key=key, details=details)
                        if hook.get("timeout") is None:
                            finding("hook-timeout-unspecified", f"{path}:{key}", "No explicit timeout; runtime default applies.")
                        if event in ("SessionStart", "UserPromptSubmit"):
                            finding("context-injection-hook", f"{path}:{key}", "Hook runs at a context-sensitive event; its output requires runtime observation.")
                        if event in ("PreToolUse", "PostToolUse") and group.get("matcher", "") in ("", "*", ".*"):
                            finding("broad-hook", f"{path}:{key}", "Hook can run for every tool call; inspect frequency and output size.")
        for name, value in data.get("enabledPlugins", {}).items():
            add(path, "plugin-registration", scope, "configured", text=str(name), key=name,
                details={"enabled": bool(value)})
        for name, value in data.get("plugins", {}).items():
            add(path, "plugin-registration", scope, "configured", text=str(name), key=name,
                details={"enabled": value.get("enabled") if isinstance(value, dict) else None})

    global_agents = codex / "AGENTS.override.md"
    if not global_agents.is_file() or not global_agents.stat().st_size:
        global_agents = codex / "AGENTS.md"
    if global_agents.is_file():
        add(global_agents, "instructions", "user", "startup-candidate")
    if (home / ".claude/CLAUDE.md").is_file():
        add(home / ".claude/CLAUDE.md", "instructions", "user", "startup-candidate")
    # Codex's project boundary is the nearest Git root. Claude ancestor instructions
    # are candidates even above that boundary; activation is client-dependent.
    ancestors = list(reversed([project, *project.parents]))
    git_root = next((p for p in [project, *project.parents] if (p / ".git").exists()), project)
    for base in ancestors:
        if base == git_root or git_root in base.parents:
            selected = base / "AGENTS.override.md"
            if not selected.is_file() or not selected.stat().st_size:
                selected = base / "AGENTS.md"
            if selected.is_file():
                add(selected, "instructions", "project", "startup-candidate")
        for name in ("CLAUDE.md", "CLAUDE.local.md", ".claude/CLAUDE.md"):
            if (base / name).is_file():
                add(base / name, "instructions", "project", "startup-candidate")
    for base, scope in ((codex, "user"), (home / ".claude", "user"),
                        (project / ".codex", "project"), (project / ".claude", "project")):
        for name in ("config.toml", "settings.json", "settings.local.json", "hooks.json"):
            config(base / name, scope, "codex" if base.name == ".codex" else "claude")
        skills(base / "skills", scope)
        for pattern in ("*.md", "*.toml"):
            files(base / "agents", pattern, "agent", scope, "on-demand")
        files(base / "rules", "**/*.md", "rule", scope)
        files(base / "commands", "**/*.md", "command", scope, "on-demand")
    config(home / ".claude.json", "user", "claude")
    config(project / ".mcp.json", "project", "claude")
    skills(home / ".agents/skills", "user")
    skills(project / ".agents/skills", "project")
    skills(codex / "skills/.system", "bundled")
    for base, scope, owner in ((home / ".pi/agent", "user", "pi"),
                                (home / ".omp/agent", "user", "omp"),
                                (project / ".pi", "project", "pi"),
                                (project / ".omp", "project", "omp")):
        for name in ("settings.json", "config.yml", "mcp.json", "hooks.json"):
            config(base / name, scope, owner)
        for name in ("AGENTS.md", "SYSTEM.md", "APPEND_SYSTEM.md"):
            if (base / name).is_file():
                add(base / name, "instructions", scope, "startup-candidate", details={"owner": owner})
        skills(base / "skills", scope)
        files(base / "prompts", "**/*.md", "prompt-template", scope, "on-demand")
        files(base / "agents", "**/*.md", "agent", scope, "on-demand")
        for folder in ("extensions", "hooks"):
            for pattern in ("*.ts", "*.js", "*/index.ts", "*/index.js"):
                files(base / folder, pattern, "extension-code", scope, "installed-unverified")
    for cache in (codex / "plugins/cache", home / ".claude/plugins/cache"):
        if cache.exists():
            for manifest_name in ("plugin.json",):
                for path in sorted(cache.glob(f"**/{manifest_name}")):
                    if path.parent.name not in (".codex-plugin", ".claude-plugin"):
                        continue
                    add(path, "plugin-manifest", "cache", "installed-unverified")
                    root = path.parent.parent
                    skills(root / "skills", "cache", "installed-unverified")
                    # A cached hook is inventory, not proof of activation.
                    if (root / "hooks/hooks.json").is_file():
                        add(root / "hooks/hooks.json", "plugin-hooks", "cache", "installed-unverified")
    for path in extra or []:
        add(path.resolve(), "context-card", "explicit", "startup-candidate")
    fingerprints = {}
    for item in items:
        if item["kind"] in ("instructions", "skill-body", "rule") and item["bytes"]:
            if item["sha256"] in fingerprints:
                finding("duplicate-content", item["id"], f"Identical to {fingerprints[item['sha256']]}; loading both could duplicate context.")
            else:
                fingerprints[item["sha256"]] = item["path"]
    totals = Counter()
    for item in items:
        totals[item["loading"]] += item["tokens"]
    return dict(schema_version=1, created_at=datetime.now(timezone.utc).isoformat(),
                project=str(project), home=str(home), tokenizer=ENCODING,
                measurement="Text token estimates, not provider billing or proof of runtime loading. Categories must not be summed as startup usage.",
                totals_by_loading=dict(totals), items=items, findings=findings, errors=errors,
                limitations=["Managed/remote policies, dynamic hooks, auto-memory, custom profiles and instruction imports are not resolved.",
                             "Skills and plugin cache entries are discovered inventory; enablement and client-specific precedence are not fully resolved.",
                             "MCP schemas are unknown until supplied; native tools and platform prompts are not counted.",
                             "Only instruction ancestors and configured directories are scanned; nested project instructions load conditionally and are not traversed."])


def compare(before: dict, after: dict) -> dict:
    old = {x["id"]: x for x in before["items"]}
    new = {x["id"]: x for x in after["items"]}
    changed = []
    for key in old.keys() & new.keys():
        if any(old[key].get(k) != new[key].get(k) for k in ("sha256", "loading", "details")):
            changed.append(dict(id=key, token_delta=new[key]["tokens"] - old[key]["tokens"],
                                before=old[key], after=new[key]))
    return dict(added=[new[k] for k in sorted(new.keys() - old.keys())],
                removed=[old[k] for k in sorted(old.keys() - new.keys())],
                changed=sorted(changed, key=lambda x: x["id"]))


def audit_tools(data: dict) -> dict:
    tools = data.get("tools", data.get("result", {}).get("tools", []))
    if not isinstance(tools, list):
        raise ValueError("Expected tools/list JSON containing a tools array")
    rows = []
    for tool in tools:
        description = tool.get("description", "")
        schema = tool.get("inputSchema", {})
        issues = []
        if not description.strip():
            issues.append("Missing description")
        if tokens(description) > 500:
            issues.append("Description exceeds 500 tokens")
        if len(description.split()) < 8:
            issues.append("Short description: check that purpose and selection criteria are clear")
        if re.search(r"\b(always|must|ignore previous|ignore all)\b", description, re.I):
            issues.append("Behavioral directive in tool description; review scope")
        for name, prop in schema.get("properties", {}).items():
            if isinstance(prop, dict) and not prop.get("description"):
                issues.append(f"Parameter {name!r} has no description")
        rows.append(dict(name=tool.get("name", "<unnamed>"),
                         tokens=tokens(json.dumps(tool, sort_keys=True, ensure_ascii=False)),
                         description_tokens=tokens(description), schema_tokens=tokens(json.dumps(schema, sort_keys=True)),
                         sha256=digest(json.dumps(tool, sort_keys=True)), findings=issues))
    descriptions = Counter(t.get("description", "").strip() for t in tools)
    return dict(tokenizer=ENCODING, tools=sorted(rows, key=lambda x: -x["tokens"]),
                serialized_tokens=tokens(json.dumps(tools, sort_keys=True, ensure_ascii=False)),
                repeated_descriptions=sum(n - 1 for d, n in descriptions.items() if d and n > 1),
                measurement="Serialized tools/list estimate; host filtering, deferred loading and provider serialization can change actual usage.")


def observe(paths: list[Path]) -> dict:
    rows, errors = [], []
    seen = set()
    for path in paths:
        try:
            with path.open(encoding="utf-8") as stream:
                for line_no, line in enumerate(stream, 1):
                    try:
                        event = json.loads(line)
                    except ValueError:
                        errors.append(dict(path=str(path), line=line_no, error="Invalid JSON"))
                        continue
                    payload = event.get("payload", {})
                    if event.get("type") == "event_msg" and payload.get("type") == "token_count":
                        info = payload.get("info") or {}
                        usage = info.get("total_token_usage")
                        if usage:
                            # Keep cumulative snapshots, never sum them across events.
                            rows.append(dict(path=str(path), line=line_no, source="codex", cumulative=True,
                                             usage=usage, last_request=info.get("last_token_usage")))
                    message = event.get("message", {})
                    if event.get("type") == "assistant" and isinstance(message, dict) and message.get("usage"):
                        key = (str(path), message.get("id") or event.get("uuid") or line_no)
                        if key in seen:
                            continue
                        seen.add(key)
                        rows.append(dict(path=str(path), line=line_no, source="claude", cumulative=False,
                                         usage=message["usage"]))
        except (OSError, UnicodeError) as exc:
            errors.append(dict(path=str(path), error=str(exc)))
    latest = {}
    claude = Counter()
    for row in rows:
        if row["cumulative"]:
            latest[row["path"]] = row["usage"]
        else:
            claude.update({k: v for k, v in row["usage"].items() if isinstance(v, int)})
    return dict(codex_latest_cumulative_by_file=latest, claude_reported_usage=dict(claude),
                observations=rows, errors=errors,
                measurement="Usage recorded by the client, not attribution to files/hooks. Codex cumulative events are not summed; Claude message IDs deduplicated per file. Cached tokens are not added to Codex input totals.")
