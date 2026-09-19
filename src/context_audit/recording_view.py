"""Offline request selector; each frame preserves request-local token accounting."""
import json

from .capture_report import report
from .recordings import requests, has_tool_definitions, lifecycle_only
from .visualize import render


def render_recording(directory, session_id=None, sequence=None):
    if sequence is not None:
        return render(report(directory, sequence, session_id))
    rows = [r for r in requests(directory) if session_id is None or r.get('session_id') == session_id]
    if not rows:
        events = lifecycle_only(directory)
        if events and (session_id is None or directory.name == f"{events[0]['client']}-{session_id}"):
            from .core import ENCODING
            notice = ('Session lifecycle observed; no model request captured yet. '
                      'Context size and contents are not measured, not zero. '
                      'If you have not sent a prompt, send one normally and run /context-audit again. '
                      'If a model has already replied, request observation is missing: check the recorder and observer. '
                      'This command does not send a model request just to populate the map.')
            return render(dict(client=events[0]['client'], path=str(directory / 'events.jsonl'), tokenizer=ENCODING,
                               view_kind='captured-pending', coverage_notice=notice, context_snapshots=[], errors=[],
                               provenance=dict(native_events=len(events), linked_blocks=0, total_blocks=0,
                                               unmatched_events=len(events), ambiguous_blocks=0),
                               lifecycle_events=[dict(event_id=e['event_id'], timestamp=e['timestamp'], source=e['source'],
                                                      observation_point=e.get('observation_point', e['event']), linked=False, blocks=[]) for e in events],
                               timeline=dict(blocks=[], total_recorded_tokens=0, by_category={}, by_source=[],
                                             unknown_token_blocks=0, measurement=notice)))
        raise ValueError('No matching captured request; no audit graph was generated.')
    if len({r.get('session_id') for r in rows}) > 1:
        raise ValueError('This recording contains multiple session identities. Pass --session-id; contexts will not be mixed.')
    pages = [dict(label='Entire observed session', preferred=True, html=render(report(directory, session_id=session_id, session_wide=True)))]
    pages += [dict(label=f"{'Startup runtime snapshot' if r['type'] == 'runtime-context' else 'Request'} {r['sequence']} · {r['timestamp']} · {'tool-bearing' if has_tool_definitions(r) else 'no tool definitions'}",
                   preferred=False, html=render(report(directory, r['sequence'], session_id))) for r in rows]
    data = json.dumps(pages).replace('<', '\\u003c').replace('>', '\\u003e').replace('&', '\\u0026')
    return '''<!doctype html><html lang="en"><meta charset="utf-8"><title>Context Audit · Recording</title>
<style>body{margin:0;background:#10141c;color:#e7edf7;font:15px system-ui}header{padding:16px}select{padding:8px;max-width:100%}iframe{border:0;width:100%;height:88vh}</style>
<header><strong>Entire observed session · oldest → newest</strong>
<details><summary>Advanced: inspect an individual request</summary><label>Diagnostic view <select id="request"></select></label></details></header>
<iframe id="view" title="Session context timeline" sandbox="allow-scripts allow-modals"></iframe>
<script id="pages" type="application/json">''' + data + '''</script><script>
const pages=JSON.parse(document.getElementById('pages').textContent), select=document.getElementById('request');
pages.forEach((p,i)=>{const o=document.createElement('option');o.value=i;o.textContent=p.label;select.append(o)});
select.onchange=()=>{document.getElementById('view').srcdoc=pages[Number(select.value)].html};
select.value=pages.findIndex(p=>p.preferred);select.onchange();</script></html>'''
