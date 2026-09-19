"""Recognize declared context structure in memory; never retain body excerpts.

Markers are evidence about content, not proof of which process injected it.
Only fixed labels and explicitly declared resource identifiers are persisted.
"""
import re


def text_parts(value):
    if isinstance(value, str):
        return [value]
    if not isinstance(value, dict):
        return []
    if isinstance(value.get("text"), str):
        return [value["text"]]
    content = value.get("content")
    if isinstance(content, str):
        return [content]
    if isinstance(content, list):
        return [part["text"] for part in content if isinstance(part, dict)
                and part.get("type") in ("text", "input_text", "output_text")
                and isinstance(part.get("text"), str)]
    return []


MARKERS = (
    (r"You are Codex", "Codex base instructions"),
    (r"You are Claude", "Claude base instructions"),
    (r"(?m)^#+ Personality", "Agent personality"),
    (r"(?m)^#+ (?:Tools|Tool use)\b|## Namespace:", "Tool instructions and declarations"),
    (r"<skills_instructions>|(?m:^## Skills\s*$)", "Skill catalog and loading rules"),
    (r"<permissions instructions>|<sandbox_mode>", "Sandbox and permission policy"),
    (r"<collaboration_mode>", "Collaboration mode"),
    (r"<multi_agent_role>|<multi_agent_mode>", "Multi-agent policy"),
    (r"You can use `spawn_agent`|You can spawn sub-agents", "Multi-agent coordination instructions"),
    (r"x-anthropic-billing-header:", "Claude client billing metadata"),
    (r"<environment_context>|<environment>", "Session environment"),
    (r"(?m)^#+ AGENTS\.md instructions", "AGENTS.md instructions"),
    (r"(?m)^#+ Codebase map", "Codebase map"),
    (r"<system-reminder>", "System reminder wrapper"),
)


def identify(value, category, role=None, identity=None):
    if category == "request-parameter":
        return {}
    body = "\n".join(text_parts(value))
    sections = [label for pattern, label in MARKERS if re.search(pattern, body)]
    # Extract identifiers only from explicit declaration syntax, not arbitrary prose.
    skills = []
    for name, path in re.findall(r"(?m)^- ([\w:.-]+):[^\n]*\(file: ([^\n)]+/SKILL\.md)\)", body):
        skills.append({"name": name, "path": path})
    namespaces = list(dict.fromkeys(re.findall(r"(?m)^## Namespace: ([\w.-]+)\s*$", body)))
    tools = list(dict.fromkeys(re.findall(r"(?m)^type ([\w]+)\s*=", body))) if namespaces else []
    if isinstance(value, dict) and isinstance(value.get('tools'), list):
        sections.insert(0, 'Nested tool catalog')
        def declarations(items, prefix=''):
            for item in items:
                if not isinstance(item, dict):
                    continue
                name = item.get('name')
                if not isinstance(name, str):
                    continue
                qualified = prefix + name
                if isinstance(item.get('tools'), list):
                    namespaces.append(qualified)
                    declarations(item['tools'], qualified + '.')
                else:
                    tools.append(qualified)
        declarations(value['tools'])
    if category == 'tool-definition' and 'Nested tool catalog' in sections:
        label = 'Tool catalog: ' + ', '.join(namespaces)
    elif category == "tool-definition":
        label = "Tool definition: " + (identity or "unnamed")
    elif sections:
        label = " · ".join(sections[:2])
    else:
        label = {"system-instructions": "Unidentified instruction block", "message": f"{(role or 'Unclassified').capitalize()} message",
                 "tool-call": "Tool call", "tool-result": "Tool result"}.get(category, category)
    return dict(label=label, sections=sections, declared_skills=skills, tool_namespaces=namespaces,
                declared_tools=tools, identification_evidence="recognized-content-markers" if sections else "request-structure",
                origin_status="Injection source not verified; declarations describe content, not execution.")
