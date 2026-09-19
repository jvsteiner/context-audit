"""Content metadata: binary payloads never count as text tokens."""
import json
import re
from .core import tokens
from .identification import identify

MEDIA = {'image', 'input_image', 'audio', 'input_audio', 'output_audio', 'video', 'document', 'input_file', 'file'}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def normalized(value):
    if isinstance(value, dict):
        return {k: normalized(v) for k, v in value.items() if k not in ('cache_control', 'signature')}
    if isinstance(value, list):
        return [normalized(v) for v in value]
    return value


def has_media(value):
    if isinstance(value, dict):
        return value.get('type') in MEDIA if isinstance(value.get('type'), str) and value.get('type') in MEDIA else any(has_media(v) for v in value.values())
    if isinstance(value, list):
        return any(has_media(v) for v in value)
    return isinstance(value, str) and value.startswith('data:') and ';base64,' in value


def parts(value, category, role, fingerprint, pointer=''):
    result = []

    def emit(item, kind, path, count, **details):
        identity = identify(item, kind, role, details.get('tool'))
        if kind == 'tool-definition' and isinstance(value, dict) and value.get('name'):
            identity = identify(item, kind, role, value['name'])
        if kind == 'media':
            identity['label'] = 'Attachment: ' + details['media']['type']
        result.append(dict(pointer=path, category=kind, role=role, fingerprint=fingerprint(normalized(item)),
                           text_tokens=count, measurement='unknown-model-cost' if count is None else 'estimated-text',
                           identification=identity, **{k: v for k, v in details.items() if v is not None}))

    def visit(item, kind, path, call_id=None):
        typ = item.get('type') if isinstance(item, dict) else None
        if typ in MEDIA or typ in ('redacted_thinking', 'reasoning'):
            source = item.get('source', {})
            media = {'type': typ}
            if isinstance(source, dict):
                if isinstance(source.get('media_type'), str):
                    media['mime_type'] = source['media_type']
                if isinstance(source.get('data'), str):
                    media['encoded_characters'] = len(source['data'])
            emit(item, 'media' if typ in MEDIA else 'reasoning', path, None, media=media, call_id=call_id)
        elif typ in ('tool_use', 'function_call', 'custom_tool_call'):
            emit(item, 'tool-call', path, None if has_media(item) else tokens(canonical(normalized(item))), call_id=item.get('id') or item.get('call_id'), tool=item.get('name'))
        elif typ in ('tool_result', 'function_call_output', 'custom_tool_call_output'):
            content = item.get('content', item.get('output', ''))
            identity = item.get('tool_use_id') or item.get('call_id')
            if isinstance(content, list):
                for i, child in enumerate(content):
                    visit(child, 'tool-result', f'{path}/content/{i}', identity)
            else:
                emit(content, 'tool-result', path, tokens(content) if isinstance(content, str) else None, call_id=identity)
        elif typ == 'thinking':
            text = item.get('thinking')
            emit(item, 'reasoning', path, tokens(text) if isinstance(text, str) else None)
        elif isinstance(item, dict) and 'content' in item:
            content = item['content']
            if isinstance(content, list):
                for i, child in enumerate(content):
                    visit(child, kind, f'{path}/content/{i}')
            elif isinstance(content, str):
                visit(content, kind, path + '/content')
            else:
                emit(item, kind, path, None)
        elif isinstance(item, str):
            chunks = re.split(r'(<system-reminder>[\s\S]*?</system-reminder>)', item) if kind == 'message' else [item]
            for i, chunk in enumerate(chunks):
                if chunk:
                    emit(chunk, 'injected' if chunk.startswith('<system-reminder>') else kind,
                         path + f'/section/{i}' if len(chunks) > 1 else path, tokens(chunk), call_id=call_id)
        elif typ in ('text', 'input_text', 'output_text') and isinstance(item.get('text'), str):
            visit(item['text'], kind, path, call_id)
        elif kind == 'tool-definition':
            emit(item, kind, path, tokens(canonical(normalized(item))))
        else:
            emit(item, kind, path, None, call_id=call_id)
    visit(value, category, pointer)
    return result
