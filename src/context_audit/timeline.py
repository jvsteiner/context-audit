"""Exclusive context blocks and provenance edges from historical records."""
import json
from collections import Counter

from .core import digest, tokens


def build_timeline(records, client):
    from .sessions import call_source, content_text

    nodes, calls, seen = [], {}, set()
    epoch = 0

    def call_details(name, args, cid):
        details = dict(tool=name, call_id=cid)
        if isinstance(args, dict):
            # Retain identifiers, never arbitrary arguments, commands or file bodies.
            if name.lower() in ("read", "read_file", "readfile", "write", "write_file", "edit", "edit_file"):
                path = args.get("file_path", args.get("path", args.get("filePath")))
                if isinstance(path, str):
                    details["file_path"] = path
                    if path.startswith("skill://"):
                        details["skill"] = path.removeprefix("skill://").split("/")[0]
                    elif path.endswith("/SKILL.md"):
                        details["skill"] = path.rsplit("/", 2)[-2]
                for key in ("offset", "limit", "start_line", "end_line"):
                    if isinstance(args.get(key), int):
                        details[key] = args[key]
            if name.lower() in ("skill", "load_skill"):
                skill = args.get("skill", args.get("name"))
                if isinstance(skill, str):
                    details["skill"] = skill
        return details

    def add(line, event, category, body="", source="unknown", evidence="recorded", parent=None, key=None, **details):
        if key is not None:
            if key in seen:
                return
            seen.add(key)
        node = dict(id=f"block-{len(nodes)}", line=line, timestamp=event.get("timestamp"),
                    category=category, source=source, evidence=evidence, parent=parent,
                    tokens=tokens(body), sha256=digest(body), epoch=epoch, details=details)
        nodes.append(node)
        return node["id"]

    def call(line, event, cid, name, args):
        sources, evidence = call_source(name, args)
        body = json.dumps(dict(name=name, arguments=args), ensure_ascii=False)
        details = call_details(name, args, cid)
        node = add(line, event, "tool-call", body, name, key=("call", cid) if cid else None, **details)
        if node:
            calls[cid] = dict(node=node, name=name, sources=sources, evidence=evidence, args=args, details=details)

    def result(line, event, cid, value, failed=False):
        caller = calls.get(cid, {})
        category, source, evidence = "tool-result", caller.get("name", "unknown tool"), "linked-call" if caller else "unlinked"
        if caller.get("evidence") == "direct-read" and not failed:
            category, source, evidence = "skill", caller["sources"][0], "direct-read"
        args = caller.get("args", {})
        if isinstance(args, dict) and caller.get("name", "").lower() in ("read", "read_file", "readfile"):
            path = args.get("file_path", args.get("path", ""))
            if path and category != "skill" and not failed:
                category, source, evidence = "file", str(path), "direct-read"
        add(line, event, category, content_text(value), source, evidence, caller.get("node"),
            key=("result", cid) if cid else None, failed=failed,
            candidate_sources=caller.get("sources", []) if caller.get("evidence") == "candidate-mixed-output" else [],
            **caller.get("details", {}))

    def text_block(line, event, role, body, key=None):
        caller = calls.get(event.get("sourceToolUseID"), {})
        if event.get("isMeta") and caller.get("evidence") == "skill-invocation":
            return add(line, event, "skill", body, caller["sources"][0], "linked-skill-injection", caller["node"], key, **caller.get("details", {}))
        category = {"user": "user", "assistant": "assistant", "developer": "instructions", "system": "instructions"}.get(role, "other")
        source = role
        # Only classify explicit harness wrappers, never arbitrary references to files.
        if body.startswith("# AGENTS.md instructions"):
            category, source = "file", body.splitlines()[0].removeprefix("# ")
        elif event.get("isMeta"):
            category, source = "injected", "client injection (owner unresolved)"
        return add(line, event, category, body, source, key=key)

    for line, event in records:
        typ = event.get("type")
        p = event.get("payload") or {}
        if typ in ("compaction", "compacted") or (typ == "system" and event.get("subtype") == "compact_boundary"):
            epoch += 1
            add(line, event, "compaction", source="client", retained_context="unknown")
            summary = event.get("summary") or p.get("message")
            if isinstance(summary, str) and summary:
                add(line, event, "summary", summary, "compactor")
        if typ == "session_meta":
            base = p.get("base_instructions", {})
            body = base.get("text", "") if isinstance(base, dict) else content_text(base)
            if body:
                add(line, event, "instructions", body, "recorded base instructions", "recorded-metadata")
        if typ == "system" and event.get("hookAdditionalContext"):
            # Logs may record output for several hooks together. Preserve candidates
            # without assigning the combined output to one arbitrary command.
            infos = event.get("hookInfos") or []
            names = [str(x.get("hookName", x.get("name", x.get("event", "unnamed hook")))) for x in infos if isinstance(x, dict)]
            add(line, event, "hook", content_text(event["hookAdditionalContext"]),
                names[0] if len(names) == 1 else "hook output (combined/unresolved)",
                "recorded-hook-context", hook_candidates=names)
        if typ == "response_item":
            subtype = p.get("type")
            if subtype in ("function_call", "custom_tool_call"):
                args = p.get("arguments", p.get("input", ""))
                if isinstance(args, str):
                    try:
                        args = json.loads(args)
                    except ValueError:
                        pass
                call(line, event, p.get("call_id"), p.get("name", "unknown"), args)
            elif subtype in ("function_call_output", "custom_tool_call_output"):
                result(line, event, p.get("call_id"), p.get("output", ""))
            elif subtype == "message":
                text_block(line, event, p.get("role", "unknown"), content_text(p.get("content")),
                           ("message", p["id"]) if p.get("id") else None)
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role", typ)
        if role == "toolResult":
            result(line, event, message.get("toolCallId"), message.get("content"), message.get("isError", False))
            continue
        content = message.get("content", [])
        if isinstance(content, str):
            text_block(line, event, role, content, ("message", event["uuid"]) if event.get("uuid") else None)
            continue
        for index, block in enumerate(content):
            if not isinstance(block, dict):
                continue
            subtype = block.get("type")
            if subtype in ("tool_use", "toolCall"):
                call(line, event, block.get("id"), block.get("name", "unknown"), block.get("input", block.get("arguments", {})))
            elif subtype == "tool_result":
                result(line, event, block.get("tool_use_id"), block.get("content"), block.get("is_error", False))
            elif subtype == "text":
                identity = event.get("uuid") or event.get("id")
                text_block(line, event, role, block.get("text", ""), ("text", identity, index) if identity else None)
            elif subtype in ("image", "image_url", "document"):
                add(line, event, "media", source=role, evidence="unmeasured-media", media_type=subtype)

    totals, source_totals = Counter(), Counter()
    cumulative = 0
    for node in nodes:
        node["start_token"] = cumulative
        cumulative += node["tokens"]
        node["end_token"] = cumulative
        totals[node["category"]] += node["tokens"]
        source_totals[(node["category"], node["source"])] += node["tokens"]
    return dict(version=1, blocks=nodes, total_recorded_tokens=cumulative, by_category=dict(totals),
                by_source=[dict(category=k[0], source=k[1], tokens=v) for k, v in source_totals.most_common()],
                measurement="Cumulative recorded text introductions across all logged branches, not a reconstructed live context window. Categories are exclusive; media and hidden context are unmeasured.")
