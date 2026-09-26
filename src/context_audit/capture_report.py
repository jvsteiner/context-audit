"""Current captured contents ordered by first observation, oldest to newest."""
import json
from collections import defaultdict, deque, Counter
from .core import ENCODING
from .recordings import has_tool_definitions
from .provenance import link_events
from .ledger import records as ledger_records


def report(directory, sequence=None, session_id=None, session_wide=False):
    records = ledger_records(directory)
    requests = [r for r in records if r.get('type') in ('request', 'runtime-context') and (session_id is None or r.get('session_id') == session_id)]
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
    events = [r for r in records if r.get('type') == 'source-event']
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
        first = next(r for r in requests if r['sequence'] == age[0])
        details['first_observation_kind'] = first['type']
        links = link_events(part, events)
        details['provenance'] = links
        # Several lifecycle observations of the same source are not competing origins.
        origins = {json.dumps(link['source'], sort_keys=True) for link in links}
        details['provenance_status'] = ('multiple-candidate-sources' if len(origins) > 1 else
                                        'source-linked' if links else 'no-native-source-link')
        if len(origins) == 1:
            native_source = links[0]['source']
            source = native_source.get('file_path') or native_source.get('skill') or native_source['label']
            details.update({k: native_source[k] for k in ('file_path', 'skill', 'tool') if k in native_source})
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
    linked = {link['event_id'] for b in blocks for link in b['details']['provenance']}
    provenance = dict(native_events=len(events), linked_blocks=sum(bool(b['details']['provenance']) for b in blocks),
                      total_blocks=len(blocks), unmatched_events=sum(e['event_id'] not in linked for e in events),
                      ambiguous_blocks=sum(b['details']['provenance_status'] == 'multiple-candidate-sources' for b in blocks))
    lifecycle = [dict(event_id=e['event_id'], event=e['event'], timestamp=e['timestamp'], source=e['source'],
                      observation_point=e.get('observation_point', e['event']), linked=e['event_id'] in linked,
                      blocks=[b['id'] for b in blocks if any(l['event_id'] == e['event_id'] for l in b['details']['provenance'])])
                 for e in events]
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
    runtime_count = sum(r['type'] == 'runtime-context' for r in requests)
    if runtime_count:
        scope = (f'{runtime_count} startup runtime snapshots and {len(requests) - runtime_count} provider requests observed. '
                 'Startup snapshots measure the effective system prompt and enabled tool schemas exposed at the observer callback, '
                 'not provider-delivered context. Later extensions, resource loading and provider serialization can change them. '
                 'Older observations appear on the left, newer on the right. Estimates are not model-window occupancy or billing. '
                 'Identical content is deduplicated across observations; changed versions remain visible. '
                 'Full source attribution is not established.')
    return dict(client=request['client'], path=str(directory / 'events.jsonl'), tokenizer=ENCODING,
                provenance=provenance, lifecycle_events=lifecycle,
                view_kind='captured-session' if session_wide else 'captured-request', coverage_notice=scope, context_snapshots=[], errors=[],
                timeline=dict(blocks=blocks, total_recorded_tokens=offset, by_category=totals, unknown_token_blocks=unknown,
                              by_source=[dict(category=b['category'], source=b['source'], tokens=b['tokens'], unknown=b['details']['token_cost_unknown']) for b in blocks], measurement=scope))
