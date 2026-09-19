# Capture feasibility — 2026-09-19

## Installed-client experiments

### Default-on authenticated smoke test

Installed the macOS LaunchAgent and persistent routing on 2026-09-19. Ordinary
`codex exec` (existing ChatGPT login) and `claude -p` (existing normal authentication,
not bare mode) each returned `OK`. Their separately identified session ledgers
contain an HTTP 200 response and a completed stream. Request serialized estimates
were 18,375 and 27,526 tokens respectively; these are test-specific estimates,
not universal startup sizes. A Codex diagnostic mentioned WebSocket transport;
the successful response was captured over HTTP. Desktop transport remains untested.

Installation starts at login, checks recorder health before changing routing, and
keeps backups. Uninstall guards owned routing fields while preserving unrelated
client updates (the smoke test added a Codex project trust entry). Tests cover
startup failure rollback, restore conflicts, session separation and restart continuity.

### Earlier rejecting-endpoint probes

Tested Codex CLI 0.155.1 and Claude Code 2.1.277 against a rejecting loopback
HTTP endpoint. No model inference was performed. Client stdout/stderr were discarded;
Context Audit persisted request metadata only.

- Codex: custom Responses provider, user config ignored, ephemeral session.
  Captured the outbound JSON body, including multiple developer instruction
  messages before the user prompt. Initial observation: about 15,800 serialized
  tokens. This path exposes instructions embedded as messages, rather than solely
  an `instructions` field. The probe request had an empty tools array.
- Claude: bare mode, dummy API key, local `ANTHROPIC_BASE_URL`, only Read enabled.
  Captured three system blocks, a user message and the Read tool definition.
  Initial observation: about 560 serialized tokens. Bare mode deliberately omits
  normal hook execution and project auto-discovery; this is not a full setup test.

The experiments demonstrate that both installed clients expose request bodies
through endpoint configuration. They DO NOT demonstrate normal subscription
authentication, the Codex desktop app, WebSocket traffic, successful inference,
source-level provenance, or complete capture across all paths.

## Implemented

- `current` rejects uncaptured sessions; `run` supplies process-scoped routing;
  `captures` lists recordings; the HTML viewer selects individual requests.
- Local installed-client probes confirmed Codex `thread-id` and Claude
  `metadata.user_id` session identity extraction. This proves association on those
  probe paths, not normal authenticated session or desktop coverage.

- `probe-capture`: reproducible local installed-client tests.
- A metadata ledger with local HMAC fingerprints, ordered components, source
  registration, request identity, and added/removed/retained component counts.
- Explicit source registration supports exact content matching. No automatic
  client-side source collector is installed yet.
- `capture-serve`: opt-in loopback HTTP gateway to a fixed HTTPS upstream. Captures
  before forwarding; passes credentials through memory without logging them;
  streams responses and records best-effort numeric usage fields.
- `capture-report`: displays the captured components of a selected request,
  including system instructions and tool definitions before/alongside messages.
- Integration test confirms byte-identical payload forwarding, streaming response
  delivery, usage association and absence of canary prompts/credentials in artifacts.

## Explicit coverage contract

`complete-json-body` means every top-level field of the received JSON object was
measured or represented by child components. It does not mean full client session
coverage, semantic token accounting, or complete source attribution. Empty arrays
are represented. Unrecognized fields are measured without retaining their values.

`attribution_status: partial` is deliberate. Serialized request bodies do not
identify all contributing hooks, files, extensions or skill catalogs. Exact source
matching cannot resolve text transformed or combined by the client. Current source
registration is an API primitive, not a working automatic hook collector.

`previous_response_id` is flagged as unresolved server context. Native/hosted tools,
encrypted reasoning and media may be represented without inspectable contents.
Component token sums differ from whole-request encoding due to framing and token
boundaries; neither is provider billing. Response usage collection is best effort.

## Work still required before a complete-audit release

1. Codex: expand beyond the successful CLI/ChatGPT smoke test to API-key and
   desktop app routing; support or instrument WebSocket/incremental context references.
2. Claude: expand beyond the successful normal-auth smoke test; exercise startup
   files, skills, hook output, MCP schemas, compaction and resumed sessions.
3. Add provenance instrumentation at source-loading/injection boundaries. Supported
   hooks must be assessed individually; a source event cannot prove final inclusion.
4. Capture response context components and link tool/skill lifecycle events across
   requests. Current gateway response logging retains usage only.
5. Prove completeness using controlled canaries for every required source and
   intentional omissions. Reject unsupported routes rather than claiming coverage.
6. Validate the connected `/context-audit` request viewer end-to-end under normal
   authenticated runs; complete Pi/OMP recording and resumed-recording aggregation.

The in-session `current` command now requires an exact session-matched recording;
there is no transcript fallback. `run` launches Codex/Claude with per-process HTTP
routing, and `captures` lists saved metadata. The recording viewer selects among
requests. `install --apply` now enables persistent default recording on macOS.
Normal authentication succeeded in the smoke tests above; desktop routing and
full injection provenance remain release gates.

## Sources

- Codex endpoint configuration: https://developers.openai.com/es-419/docs/config-file/config-advanced
- Claude gateway support: https://code.claude.com/docs/en/llm-gateway
- Claude hook reference: https://code.claude.com/docs/en/hooks
- Installed `codex exec --help` and `claude --help` for exact-version probe flags.
