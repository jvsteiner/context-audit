"""Normalize native lifecycle observations without persisting their bodies."""
from datetime import datetime, timezone
import uuid

from .content_parts import parts

EVENTS = {'SessionStart', 'SessionEnd', 'InstructionsLoaded', 'UserPromptSubmit',
          'PostToolUse', 'PostToolBatch', 'PreCompact', 'PostCompact', 'SubagentStart', 'SubagentStop',
          'session_start', 'session_shutdown', 'before_agent_start', 'tool_result', 'session_compact', 'message_end'}


def identifier(value):
    return value if isinstance(value, str) and len(value) <= 4096 else None


def record_event(store, payload):
    if not isinstance(payload, dict):
        raise ValueError('Lifecycle payload must be an object')
    event = payload.get('hook_event_name') or payload.get('type')
    if event not in EVENTS:
        raise ValueError('Unsupported lifecycle event')
    if event == 'PostToolBatch':
        batch = payload.get('tool_calls')
        if not isinstance(batch, list):
            raise ValueError('Invalid tool batch')
        return [record_event(store, {**entry, 'hook_event_name': 'PostToolUse', '_model_facing': True})
                for entry in batch if isinstance(entry, dict)]
    tool = identifier(payload.get('tool_name') or payload.get('toolName'))
    call_id = identifier(payload.get('tool_use_id') or payload.get('toolCallId'))
    inputs = payload.get('tool_input', payload.get('input', {}))
    inputs = inputs if isinstance(inputs, dict) else {}
    source = {'kind': 'lifecycle', 'label': event}
    if event == 'InstructionsLoaded':
        source = {'kind': 'instruction-file', 'label': 'Instruction loaded'}
        for key in ('file_path', 'trigger_file_path', 'parent_file_path', 'load_reason', 'memory_type'):
            if identifier(payload.get(key)):
                source[key] = payload[key]
    elif tool:
        source = {'kind': 'tool', 'label': tool, 'tool': tool}
        if tool.lower() in ('read', 'read_file'):
            path = identifier(inputs.get('file_path') or inputs.get('path'))
            if path:
                source.update(kind='skill' if path.endswith('/SKILL.md') or path.startswith('skill://') else 'file', file_path=path)
        if tool.lower() == 'skill' and identifier(inputs.get('skill')):
            source.update(kind='skill', skill=inputs['skill'])
        if tool.startswith('mcp__') and len(tool.split('__')) >= 3:
            source['declared_server'] = tool.split('__')[1]
    content = payload.get('tool_response', payload.get('content')) if tool else None
    if event == 'UserPromptSubmit':
        content = payload.get('prompt')
        source = {'kind': 'user-input', 'label': 'Submitted user prompt'}
    if event == 'before_agent_start':
        content = [{'type': 'text', 'text': s} for s in payload.get('systemPrompt', []) if isinstance(s, str)]
        source = {'kind': 'assembled-instructions', 'label': 'OMP assembled system prompt at extension callback'}
    observed = parts({'content': content} if isinstance(content, list) else content, 'tool-result', None, store.fingerprint) if content is not None else []
    row = dict(type='source-event', schema_version=1, event_id=str(uuid.uuid4()), event=event,
               timestamp=datetime.now(timezone.utc).isoformat(), client=store.client,
               source=source, call_id=call_id, evidence='native-lifecycle-event',
               observation_point='PostToolBatch' if payload.get('_model_facing') else event,
               content_fingerprints=[p['fingerprint'] for p in observed],
               content_tokens=sum(p['text_tokens'] or 0 for p in observed) if content is not None else None,
               unknown_cost_parts=sum(p['text_tokens'] is None for p in observed),
               failed=bool(payload.get('isError') or payload.get('is_error')))
    with store.lock:
        store.append(row)
    return row


def link_events(part, events):
    result = []
    for event in events:
        call = part.get('call_id') and part.get('call_id') == event.get('call_id')
        exact = part['fingerprint'] in event.get('content_fingerprints', [])
        # Equal short text in unrelated tool results is not evidence of common origin.
        if part.get('call_id') and event.get('call_id') and not call:
            continue
        if call or exact:
            result.append(dict(event_id=event['event_id'], event=event['event'], source=event['source'],
                               timestamp=event['timestamp'], observation_point=event.get('observation_point', event['event']),
                               evidence='exact-content-and-call-id' if exact and call else 'exact-content-match' if exact else 'call-id-match',
                               failed=event.get('failed', False)))
    return result
