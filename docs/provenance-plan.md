# Cross-client provenance implementation plan

## Research — 2026-09-19

Versions inspected: Codex CLI 0.155.1, Claude Code 2.1.278, OMP 18.2.6.

| Client | Native evidence | Boundary and limits |
| --- | --- | --- |
| Claude | `InstructionsLoaded`: path, load reason, triggering/parent file. `PostToolUse`: tool identity/input/output. `PostToolBatch`: model-facing tool results. | Instruction events omit bodies; structured tool output may differ from model-facing output. Direct AGENTS.md loads are not covered by InstructionsLoaded. |
| Codex | `PostToolUse`: tool name, call ID, output; session and compaction events. User `hooks.json` is supported. | Hooks need explicit trust. No documented InstructionsLoaded equivalent. Nested code-mode tool results may be transformed before reaching the model. |
| OMP | Installed extension types expose `before_provider_request`, `before_agent_start`, `tool_result`, session and compaction events. | Observe without returning replacements. Earlier extension handlers may already have changed content; provider support and ordering must remain explicit. |

Primary references:
- [Claude hook reference](https://code.claude.com/docs/en/hooks)
- [Codex hook reference](https://learn.chatgpt.com/docs/hooks)
- [OMP extension documentation](https://github.com/can1357/oh-my-pi/blob/main/docs/extensions.md)
- Installed OMP `src/extensibility/extensions/types.ts` and `src/extensibility/hooks/types.ts`.

## Shared evidence contract

Use session-local event IDs, timestamps, client and capability identifiers,
allowlisted source metadata, call IDs, and keyed content fingerprints. Never save
raw input/output, commands, credentials, or prompt bodies. Keep these separate:

1. A resource is configured or declared.
2. A native event reports a resource/tool operation.
3. Its content matches a captured component.
4. An injection origin is inferred only from markers (not verified).

A tool-call-ID edge establishes which operation returned content, not that no
later hook modified it. Exact normalized content matches are stronger evidence.
An instruction-load event without a content match is shown as an event, not
silently assigned the cost of a whole mixed prompt. Multiple matches remain
ambiguous. Match events even if asynchronous delivery follows request capture.

## Implementation sequence

1. Add a local ingestion endpoint to the existing recorder. Normalize native
   events into private per-session ledgers; use the same fingerprint key as
   request capture. One daemon owns all writes. Validate client/session identity.
2. Add an observer command receiving hook JSON over stdin. It emits no hook
   output and never changes decisions. Bound transport waits; report collection
   failure separately, without blocking normal agent work.
3. Add Claude/Codex hook configurations and an OMP observer extension. Preview,
   backup and guard configuration edits; do not bypass Codex trust. OMP provider
   callbacks submit exact payloads in memory, enabling capture without proxying
   each provider. No provider secrets are persisted.
4. Link captured parts to native events by call ID and/or keyed content match.
   Expose source kind, file/skill identifier, trigger/load reason and evidence in
   the block inspector. Show unmatched events in a separate lifecycle panel.
5. Record tested capability/coverage boundaries for each client. Test privacy,
   duplicate/ambiguous content, asynchronous event order, negative matches,
   session isolation, installer restore and non-mutating OMP callbacks.
6. Smoke-test supported installed-client paths, regenerate reports, commit and
   push implementation. Never mark trust-pending or untested coverage complete.

## Explicit follow-on work, not claimed by this increment

Generic sibling hooks do not observe other hooks' stdout. Complete hook-output
causality needs separately approved, compatibility-tested hook wrappers or a
client-maintained event API. MCP schema ownership should distinguish encoded
server names from verified plugin registrations. Source content embedded into
mixed instructions needs span-level matching, not total-block attribution.
Codex desktop/WebSocket and providers that omit OMP callbacks need dedicated tests.

## Implementation and validation outcome

Implemented the metadata-only lifecycle ingress, Claude/Codex observer command,
OMP native callback observer, guarded installation/removal, report-time evidence
links, and unmatched-event inspector. The default whole-session chronology is
unchanged. Events delivered after requests can still be matched at report time.

Validation on the installed versions listed above:

- Claude: a normal fresh session loaded an instruction file, submitted a prompt,
  and read the project license. The model-facing batch output matched the captured
  tool result exactly; the instruction-load event remained separately visible.
- OMP: a normal fresh session captured two provider requests. The assembled system
  prompt and license read result matched; callbacks did not replace native payloads.
- Codex: normal authenticated requests completed. New native observer events were
  not observed. Hooks are installed but activation remains unverified; review them
  using `/hooks`. No trust override or trust-database changes were made.
- Offline browser: lifecycle event expansion and linked block inspector worked
  without JavaScript errors on the real Claude recording.
- Python regression suite plus Bun callback non-mutation/failure test and wheel
  build pass. Fixtures cover Codex Responses outputs, Claude batches, OMP system
  content, late events, transformed outputs, ambiguous matches, privacy, session
  isolation, and reversible configuration edits.

The Codex smoke test also exposed rejected model-catalog GET requests. A narrowly
scoped `/models` passthrough now preserves catalog refresh without treating it as
model context or writing credentials/bodies. Other GET/WebSocket paths are not
silently forwarded as uncaptured inference.

This increment is not a complete causal audit: sibling hook output, mixed-source
instruction spans, unobserved provider/server-side additions, unsupported callback
paths, and untrusted hooks remain explicit release gates.

### OMP pre-request startup snapshot

Installed OMP 18.2.6 exposes `ctx.getSystemPrompt()` and
`pi.getActiveTools()` / `pi.getAllTools()` after extension initialization. The
observer now snapshots the effective system prompt and enabled tool schemas at
`session_start`, without initiating inference. It stores metadata using a separate
`runtime-context` record type, distinguishes these from provider requests in
discovery and the viewer, and preserves their observed age as later requests arrive.
Other extension handlers or resources loaded later can still change these values.

A real RPC startup test with no prompt produced one runtime snapshot containing
49 blocks and 22,073 estimated text/schema tokens, with zero provider requests.
The daemon and installed OMP observer were updated; an already-running session
needs extension reload to take a snapshot of its current runtime state.
