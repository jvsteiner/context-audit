"""Historical attribution from recorded content; never re-read current skill files."""
from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

from .core import ENCODING, digest, tokens


def content_text(value) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(content_text(x) for x in value)
    if isinstance(value, dict):
        return str(value.get("text", ""))
    return ""


def discover(home: Path, client: str = "all", limit: int = 30) -> list[dict]:
    locations = {"codex": home / ".codex/sessions", "claude": home / ".claude/projects",
                 "pi": home / ".pi/agent/sessions", "omp": home / ".omp/agent/sessions"}
    rows = []
    for name, root in locations.items():
        if client not in ("all", name) or not root.exists():
            continue
        for path in root.rglob("*.jsonl"):
            stat = path.stat()
            rows.append(dict(client=name, path=str(path), bytes=stat.st_size, modified=stat.st_mtime))
    return sorted(rows, key=lambda x: -x["modified"])[:limit]


def call_source(name: str, args) -> tuple[list[str], str]:
    """Return evidence candidates and confidence, not guessed full-file sizes."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            pass
    if isinstance(args, dict):
        path = args.get("file_path", args.get("path", args.get("filePath", "")))
        if isinstance(path, str) and (path.endswith("SKILL.md") or path.startswith("skill://")) and name.lower() in ("read", "read_file", "readfile"):
            return [path], "direct-read"
        if name.lower() in ("skill", "load_skill"):
            return ["skill:" + str(args.get("skill", args.get("name", "unknown")))], "skill-invocation"
        raw = json.dumps(args)
    else:
        raw = str(args)
    # Shell/code orchestration can contain multiple reads, output truncation or
    # an unrelated mention. Never classify the whole result as skill content.
    paths = sorted(set(re.findall(r"(?:/|~/)[^\s\"'`<>;(){}]+/SKILL\.md", raw)))
    return paths, "candidate-mixed-output" if paths else "unattributed"


def analyze(path: Path, client: str = "auto") -> dict:
    records, errors = [], []
    with path.open(encoding="utf-8") as stream:
        for line_no, line in enumerate(stream, 1):
            try:
                event = json.loads(line)
                if not isinstance(event, dict):
                    raise ValueError("Expected object")
                records.append((line_no, event))
            except ValueError:
                errors.append(dict(line=line_no, error="Invalid JSON object"))
    if client == "auto":
        if any(e.get("type") == "session_meta" for _, e in records):
            client = "codex"
        elif any(e.get("type") == "session" for _, e in records):
            client = "omp" if ".omp" in path.parts or any(e.get("message", {}).get("contextSnapshot") for _, e in records) else "pi"
        else:
            client = "claude"
    calls, loads, requests, compactions, hook_context, snapshots, startup = {}, [], {}, [], [], [], []
    seen_results, seen_injections = set(), set()
    epoch = 0
    legacy_cumulative = None
    message_tokens = Counter()

    def register(call_id, name, args, line):
        sources, evidence = call_source(name, args)
        calls[call_id] = dict(tool=name, sources=sources, evidence=evidence, line=line)

    def result(call_id, value, line, failed=False):
        if call_id in seen_results:
            return
        seen_results.add(call_id)
        body = content_text(value)
        size = tokens(body)
        message_tokens["tool-results"] += size
        call = calls.get(call_id)
        if not call or not call["sources"]:
            return
        loads.append(dict(call_id=call_id, line=line, call_line=call["line"], tool=call["tool"],
                          sources=call["sources"], evidence=call["evidence"], failed=failed,
                          compaction_epoch=epoch, returned_text_tokens=size,
                          attributed_skill_tokens=size if call["evidence"] == "direct-read" and not failed else None,
                          sha256=digest(body)))

    for line, event in records:
        typ = event.get("type")
        payload = event.get("payload") or {}
        if typ in ("compaction", "compacted") or (typ == "system" and event.get("subtype") == "compact_boundary"):
            epoch += 1
            compactions.append(dict(line=line, epoch=epoch))
        if typ == "session_meta":
            base = payload.get("base_instructions", {})
            body = base.get("text", "") if isinstance(base, dict) else content_text(base)
            if body:
                startup.append(dict(line=line, kind="recorded-base-instructions", tokens=tokens(body)))
        if typ == "token_usage_record":
            key = payload.get("response_id") or f"line:{line}"
            requests[key] = dict(line=line, usage=payload.get("usage", {}), source="provider-record")
        if typ == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info") or {}
            legacy_cumulative = info.get("total_token_usage") or legacy_cumulative
        if typ == "system" and event.get("hookAdditionalContext"):
            body = content_text(event["hookAdditionalContext"])
            hook_context.append(dict(line=line, tokens=tokens(body), epoch=epoch, evidence="recorded-hook-context"))
        if typ == "response_item":
            ptype = payload.get("type")
            if ptype in ("function_call", "custom_tool_call"):
                register(payload.get("call_id"), payload.get("name", ""), payload.get("arguments", payload.get("input", "")), line)
            elif ptype in ("function_call_output", "custom_tool_call_output"):
                result(payload.get("call_id"), payload.get("output", ""), line)
            elif ptype == "message":
                body = content_text(payload.get("content", ""))
                role = payload.get("role", "unknown")
                message_tokens[role] += tokens(body)
                if role == "developer":
                    startup.append(dict(line=line, kind="recorded-developer-message", tokens=tokens(body)))
            continue
        message = event.get("message")
        if not isinstance(message, dict):
            continue
        role = message.get("role", typ)
        # Claude's Skill tool returns an acknowledgement; the actual skill arrives
        # in a separate meta user message linked by sourceToolUseID.
        source_call = event.get("sourceToolUseID")
        if event.get("isMeta") and source_call in calls and calls[source_call]["evidence"] == "skill-invocation":
            identity = event.get("uuid") or f"line:{line}"
            if identity not in seen_injections:
                seen_injections.add(identity)
                body = content_text(message.get("content", ""))
                call = calls[source_call]
                loads.append(dict(call_id=source_call, line=line, call_line=call["line"], tool=call["tool"],
                                  sources=call["sources"], evidence="linked-skill-injection", failed=False,
                                  compaction_epoch=epoch, returned_text_tokens=tokens(body),
                                  attributed_skill_tokens=tokens(body), sha256=digest(body)))
        if role == "assistant" and message.get("usage"):
            key = message.get("id") or message.get("responseId") or event.get("id") or event.get("uuid") or f"line:{line}"
            # Claude writes several content blocks for one response. Last usage
            # wins; first-block usage can be incomplete.
            requests[key] = dict(line=line, usage=message["usage"], source="message-usage")
        if message.get("contextSnapshot"):
            snapshots.append(dict(line=line, **{k: v for k, v in message["contextSnapshot"].items()
                                               if k in ("promptTokens", "nonMessageTokens", "compactionEpoch") and isinstance(v, int)}))
        if role == "toolResult":
            result(message.get("toolCallId"), message.get("content"), line, message.get("isError", False))
            continue
        blocks = message.get("content", [])
        if isinstance(blocks, str):
            message_tokens[role] += tokens(blocks)
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            if block.get("type") in ("tool_use", "toolCall"):
                register(block.get("id"), block.get("name", ""), block.get("input", block.get("arguments", {})), line)
            elif block.get("type") == "tool_result":
                result(block.get("tool_use_id"), block.get("content"), line, block.get("is_error", False))
            elif block.get("type") == "text":
                message_tokens[role] += tokens(block.get("text", ""))

    skill_totals = {}
    for load in loads:
        for source in load["sources"]:
            row = skill_totals.setdefault(source, dict(source=source, reads=0, failed_reads=0,
                                                       attributed_returned_tokens=0, ambiguous_reads=0))
            row["reads"] += 1
            row["failed_reads"] += int(load["failed"])
            if load["attributed_skill_tokens"] is not None:
                row["attributed_returned_tokens"] += load["attributed_skill_tokens"]
            else:
                row["ambiguous_reads"] += 1
    usage_totals = Counter()
    for request in requests.values():
        usage_totals.update({k: v for k, v in request["usage"].items() if isinstance(v, int) and not isinstance(v, bool)})
    from .timeline import build_timeline
    return dict(schema_version=1, client=client, path=str(path), tokenizer=ENCODING,
                timeline=build_timeline(records, client),
                skill_loads=loads, skills=sorted(skill_totals.values(), key=lambda x: -x["attributed_returned_tokens"]),
                attributed_skill_returned_tokens=sum(x["attributed_skill_tokens"] or 0 for x in loads),
                ambiguous_skill_outputs=sum(x["attributed_skill_tokens"] is None for x in loads),
                recorded_text_tokens_by_role=dict(message_tokens), recorded_instruction_components=startup,
                hook_context=hook_context, context_snapshots=snapshots, compactions=compactions,
                request_count=len(requests), reported_request_usage=dict(usage_totals),
                latest_legacy_cumulative=legacy_cumulative, requests=list(requests.values()), errors=errors,
                limitations=["Skill attribution measures recorded returned text, including tool framing; not exact model-token billing or semantic skill influence.",
                             "Shell/orchestrator output mentioning a skill is ambiguous and excluded from attributed totals.",
                             "No skill output is multiplied by later requests: compaction, pruning, caching and branching require request-level reconstruction.",
                             "This audits all recorded events, including abandoned branches; it does not reconstruct a final active branch.",
                             "Missing skill events do not prove zero skill context: startup catalogs and unlinked injected content may not be logged.",
                             "Provider usage fields are retained separately; their cache semantics differ and fields must not be blindly added."])
