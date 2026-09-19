"""Current captured contents ordered by first observation, oldest to newest."""
import json
from collections import defaultdict, deque, Counter
from .core import ENCODING
from .recordings import has_tool_definitions


def report(directory, sequence=None, session_id=None, session_wide=False):
    records = [json.loads(line) for line in (directory / 'events.jsonl').read_text().splitlines(keepends=True) if line.endswith('\n')]
    requests = [r for r in records if r.get('type') == 'request' and (session_id is None or r.get('session_id') == session_id)]
    if sequence is not None:
        requests = [r for r in requests if r['sequence'] <= sequence]
    if not requests or (sequence is not None and requests[-1]['sequence'] != sequence):
        raise ValueError('No matching captured request')
    request = requests[-1]
    requests = [r for r in requests if r.get('session_id') == request.get('session_id')]
    tool_stream = has_tool_definitions(request)
    if not session_wide:
        requests = [r for r in requests if has_tool_definitions(r) == tool_stream]
    sources = {r['source_id']: r for r in records if r.get('type') == 'source'}
    previous = defaultdict(deque)
    history, sightings = {}, defaultdict(list)
    for row in requests:
        expanded = []
        for component in row['components']:
            if component['category'] in ('request-parameter', 'empty-container'):
                continue
            children = component.get('parts')
            if children is None:
                known = component['category'] == 'tool-definition' or component['pointer'].startswith(('/system/', '/instructions'))
                children = [dict(pointer=component['pointer'], category=component['category'], role=component.get('role'),
                                 fingerprint=component['fingerprint'], text_tokens=component['serialized_tokens'] if known else None,
                                 measurement='legacy-schema-estimate' if known else 'legacy-unsplit-unknown', identification=component.get('identification', {}))]
            expanded.extend((component, child) for child in children)
        def priority(pair):
            c, _ = pair
            return 0 if c['pointer'].startswith(('/system', '/instructions')) else 1 if c['category'] == 'tool-definition' else 2
        expanded.sort(key=priority)
        active, next_previous = [], defaultdict(deque)
        occurrences = Counter()
        for index, (component, part) in enumerate(expanded):
            key = (part['category'], part.get('role'), part['fingerprint'])
            occurrence = (*key, occurrences[key])
            occurrences[key] += 1
            age = previous[key].popleft() if previous[key] else (row['sequence'], index, row['timestamp'])
            if session_wide:
                age = history[occurrence][0] if occurrence in history else (row['sequence'], index, row['timestamp'])
                history[occurrence] = (age, component, part)
                sightings[age[:2]].append(row['sequence'])
            next_previous[key].append(age)
            active.append((age, component, part))
        previous = next_previous
    if session_wide:
        active = list(history.values())
    active.sort(key=lambda x: x[0][:2])
    blocks, totals, offset = [], {}, 0
    for age, component, part in active:
        category = {'system-instructions': 'instructions'}.get(part['category'], part['category'])
        if category == 'message':
            category = part.get('role') if part.get('role') in ('user', 'assistant') else 'other'
        size = part['text_tokens']
        identification = part.get('identification', {})
        source = identification.get('label') or component.get('identity') or 'Unidentified component'
        matched = [sources[s] for s in component['source_ids'] if s in sources]
        if matched:
            source = ' / '.join(f"{s['kind']}: {s['identifier']}" for s in matched)
        details = dict(identification=identification, json_pointer=part['pointer'], request_id=request['request_id'],
                       source_ids=component['source_ids'], fingerprint_type='HMAC-SHA256', first_seen_request=age[0],
                       measurement=part['measurement'], token_cost_unknown=size is None)
        details.update({k: part[k] for k in ('media', 'call_id', 'tool') if k in part})
        if session_wide:
            seen = sightings[age[:2]]
            details.update(observed_in_requests=seen, last_seen_request=seen[-1],
                           request_id=next(r['request_id'] for r in requests if r['sequence'] == seen[-1]),
                           retention='Present in latest captured request' if request['sequence'] in seen else 'Previously observed; absence from latest request does not prove eviction')
        blocks.append(dict(id=f'block-{len(blocks)}', category=category, source=source, evidence=component['attribution'],
                           tokens=size or 0, start_token=offset, end_token=offset + (size or 0), line=0, epoch=0,
                           timestamp=age[2], parent=None, sha256=part['fingerprint'], details=details))
        offset += size or 0
        totals[category] = totals.get(category, 0) + (size or 0)
    calls = {b['details']['call_id']: b for b in blocks if b['category'] == 'tool-call' and b['details'].get('call_id')}
    for b in blocks:
        if b['category'] in ('tool-result', 'media') and b['details'].get('call_id') in calls:
            origin = calls[b['details']['call_id']]
            b['parent'] = origin['id']
            if origin['details'].get('tool'):
                b['details']['tool'] = origin['details']['tool']
    unknown = sum(b['details']['token_cost_unknown'] for b in blocks)
    scope = (f"Captured request #{request['sequence']}. Oldest observed content on the left; newer additions on the right. "
             'Only components present in this request are shown. Unchanged components keep their observed age; changed or reintroduced components are new observations. '
             'For components first seen together, system instructions and tool definitions precede message order. This is an observation timeline, not verified model-internal order. '
             f'{unknown} blocks have unknown token cost and are excluded from the measured total. Images and binary encodings are not text tokens. '
             'Legacy unsplit messages cannot be measured safely; a new capture supplies content-level metadata. '
             'Counts are text/schema estimates, not provider billing. Full injection attribution remains incomplete.')
    scope += (' Chronology is matched within tool-bearing requests only; intervening tool-free requests do not reset context age.'
              if tool_stream else ' This request has no tool definitions. It may be an auxiliary call or a tool-free conversation turn; purpose is not established. Chronology is matched within tool-free requests only.')
    if session_wide:
        scope = (f"Session-wide context from all {len(requests)} captured requests, oldest observed → newest observed. "
                 'A later small request never replaces or clears this view. Repeated content is matched by fingerprint and occurrence within each request; snapshots are not simply added together. '
                 'All previously observed components remain visible, including older versions and possible auxiliary-call content. '
                 'The measured total is accumulated distinct observed text/schema content—not a claim of current model-window occupancy or billing. '
                 'Click a block for the requests containing it. Absence from a request does not establish eviction or compaction. '
                 f'{unknown} unknown-cost blocks are excluded from the measured total. Image encodings are not text tokens. '
                 'Only captured request contents are represented; unrecorded output, server-side context and complete injection provenance remain unresolved.')
    return dict(client=request['client'], path=str(directory / 'events.jsonl'), tokenizer=ENCODING,
                view_kind='captured-session' if session_wide else 'captured-request', coverage_notice=scope, context_snapshots=[], errors=[],
                timeline=dict(blocks=blocks, total_recorded_tokens=offset, by_category=totals, unknown_token_blocks=unknown,
                              by_source=[dict(category=b['category'], source=b['source'], tokens=b['tokens'], unknown=b['details']['token_cost_unknown']) for b in blocks], measurement=scope))
