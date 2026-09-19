# context-audit

Understand what shapes your coding agent's context: instructions, skills, tools,
plugins, hooks, and recorded token usage. Track changes and make reversible edits.

This is an initial working CLI for **Codex, Claude Code, Pi and OMP**. It separates
discovered installation, configured activation, estimated text size, and recorded
usage. It does not equate the sum of installed files with an agent's startup prompt.

**Coverage warning:** transcript views are incomplete context reconstructions.
Startup context can be absent from client logs. The new outbound-request capture
recorder can now be enabled by a one-time installation; it is not yet a complete source audit. See
[capture feasibility and remaining release gates](docs/capture-feasibility.md).

## Default recording on macOS

From any directory, preview and apply the one-time installation:

```sh
uv run --project ~/Code/context-audit context-audit install
uv run --project ~/Code/context-audit context-audit install --apply
```

The installer detects the Codex login method using `codex login status` (or accepts
`--codex-auth chatgpt` / `--codex-auth api-key`). It refuses existing custom routing
instead of replacing it. It adds missing in-session commands, starts a user
LaunchAgent, health-checks both loopback endpoints, then updates only the default
Codex provider and Claude's `env.ANTHROPIC_BASE_URL`. No shell wrapper is required:
launch **`codex` or `claude` normally** after installation. Restart existing clients
to ensure they load the new routing. Use `$context-audit` in Codex and
`/context-audit` in Claude to open the exact session's recorded request snapshots.

The recorder starts at login and launchd restarts it if it exits. Both settings
files are backed up privately. Existing hooks and unrelated settings are retained.
Provider credentials and request bodies pass through memory, not recorder logs.
If the recorder is unavailable, routed requests fail; they are not silently sent
without recording. Higher-priority project/profile/CLI overrides may still bypass
the default routing. Pi/OMP automatic capture and Codex desktop validation are
not implemented. Hook/file source attribution remains incomplete.

```sh
uv run --project ~/Code/context-audit context-audit status
uv run --project ~/Code/context-audit context-audit captures
uv run --project ~/Code/context-audit context-audit uninstall          # preview
uv run --project ~/Code/context-audit context-audit uninstall --apply
```

Uninstall checks the routing fields it owns, restores original routing while
preserving unrelated subsequent settings, then stops the recorder. Conflicting
routing edits are not overwritten. Recordings, reports and backups are retained.

- Session metadata: `~/.context-audit/captures/CLIENT-SESSION_ID/`
- HTML reports: `~/.context-audit/reports/`
- Private settings backups: `~/.context-audit/backups/`
- Service config and install receipt: `~/.context-audit/{service,installation}.json`
- Login service: `~/Library/LaunchAgents/local.context-audit.recorder.plist`

The service runs Python from this checkout's virtual environment; keep the
checkout and `.venv` in place while installed. The default capture path continues
a session's ledger after recorder restarts. Reports offer a request selector,
not an additive sum of every request into a context window.

## Request capture prototype

### Context ordering and token measurements

The default is the **entire observed session**, not a selected HTTP request.
Context is accumulated across all captured requests; repeated content is matched
by fingerprint and occurrence count, rather than summing whole snapshots. Older
versions remain visible, newer observations appear on the right, and a small
auxiliary request cannot clear the graph. This is accumulated observed context,
not a claim that every historical block is still retained by the provider.
Click a block for its request-presence evidence. Individual request snapshots
remain available only under advanced diagnostics (or explicit `--sequence`).
Recordings containing multiple session identities require `--session-id` so their
contexts are not mixed.

Advanced captured-request graphs order the content present in the selected request by
its first observation, oldest on the left and newest on the right. Unchanged
components retain their age across consecutive snapshots; changed or reintroduced
components get a new observation. For simultaneous first observations, system
instructions and tool definitions precede the message sequence. This tie-break
does not assert the provider's internal prompt layout or exact injection times.

Capture schema v2 separates message text, tool calls/results, reasoning and media.
Text estimates exclude image base64 and reasoning signatures. Attachments show
metadata and unknown model-token cost, not zero cost. API control parameters are
not part of the context graph. The ledger's `serialized_request_tokens` field is
a wire-JSON diagnostic only and is no longer used as a context size in the viewer.

Older unsplit message metadata cannot safely distinguish text from binary data;
those blocks now show unknown cost rather than their former inflated JSON-token
estimate. Generate another request with the updated recorder for content-level
measurements. Graphs with unknown costs use event spacing so every unknown block
remains visible; the measured total excludes those blocks and is not a full total.

`current` and the installed in-session commands now require an exact matching
metadata recording. If none exists, they report **This session was not captured**
and do not open a transcript graph. `visualize` remains an explicitly partial
transcript explorer.

Launch a new recorded CLI session from your project directory:

```sh
# Direct Anthropic endpoint; preserves the client's existing authentication
uv run --project ~/Code/context-audit context-audit run --client claude --upstream https://api.anthropic.com

# Codex with ChatGPT authentication; explicit HTTP provider override for this launch
uv run --project ~/Code/context-audit context-audit run --client codex --auth chatgpt --upstream https://chatgpt.com/backend-api/codex

# Codex with an API key already available in OPENAI_API_KEY
uv run --project ~/Code/context-audit context-audit run --client codex --auth api-key --upstream https://api.openai.com/v1
```

These launchers are experimental, not validated production authentication paths.
They preserve normal startup behavior (no bare mode) and modify only the child
process environment/CLI arguments, not persistent client settings. The gateway
stays alive until the client exits. Client arguments follow `--`. Endpoint/provider
overrides in those arguments can bypass capture; the tool does not establish
network-wide interception. Desktop, remote clients and Pi/OMP recording are not
supported by this launcher yet. `run` itself installs no aliases or background
service; use `install --apply` for default recording instead.

Metadata is written under `~/.context-audit/captures/CLIENT-UUID/events.jsonl` with
a private `fingerprint.key`. Session identity comes from request metadata, not
directory recency. Subagent thread identities are kept separate. Unknown identities
cannot be opened via `current`. Multiple recordings for a resumed session require
explicit selection rather than silently choosing one.

```sh
uv run --project ~/Code/context-audit context-audit captures
uv run --project ~/Code/context-audit context-audit capture-report /path/from/list --output recording.html
```

The saved recording viewer has a request selector and defaults to the latest
captured request. Use `--session-id ID` to isolate a thread or `--sequence N` for
one request. Request token counts are not summed into a session context size.

Test your installed clients against a local rejecting sink (no model inference):

```sh
uv run context-audit probe-capture --output-dir .context-audit/probe
uv run context-audit capture-report .context-audit/probe/codex --output codex-request.html
uv run context-audit capture-report .context-audit/probe/claude --output claude-request.html
```

For explicit HTTP gateway experiments, `capture-serve --client CLIENT --upstream
HTTPS_BASE_URL --output-dir DIRECTORY` prints a loopback endpoint. It does not
change client settings. Route one client session explicitly to that endpoint using
the client's supported provider configuration. WebSockets, compressed requests,
redirects and non-POST routes are not supported. Existing auth/endpoint settings
must not be overwritten blindly. `capture-serve` does not install automatic setup.

Raw request/response text and headers are processed in memory only. Artifacts store
keyed fingerprints, counts, types, tool names and coverage metadata. The fingerprint
key and artifacts are private files. Source names and identifiers are metadata and
can still be sensitive. Provider usage is recorded separately from tokenizer estimates.

## Run

### From a live agent session

`install --apply` includes missing client integrations. To install only the
commands without enabling recording:

```sh
uv run --project ~/Code/context-audit context-audit install-integrations
```

- **Claude Code:** `/context-audit` invokes a skill using the current session ID.
- **Pi / OMP:** `/context-audit` runs a native extension using the active transcript
  path, without a model turn. Run `/reload` or restart to load the extension.
- **Codex:** invoke `$context-audit` (or select it through the skill menu). Restart
  the client if the new skill is not discovered. It resolves `CODEX_THREAD_ID`.

Each invocation requires captured requests for the exact session. It opens a
snapshot with request selection, not a live watcher. Claude/Codex skill invocation
itself can add context. Pi/OMP currently reports that recording is not connected.
Missing identity or capture is an error, never a fallback to a native transcript.

The `install-integrations` command adds only four skill/extension files in your user directories;
it does not edit existing client settings. Existing differing files are preserved.
Remove those four files listed by the installer to uninstall the commands.

The underlying command is also available to integrations:

```sh
context-audit current --client codex
context-audit current --client claude --session-id SESSION_ID
context-audit current --client omp --session-file /path/to/active.jsonl
```

To choose a session and open its visual map, run this from **any directory**:

```sh
uv run --project ~/Code/context-audit context-audit visualize
```

Select a number from the session list. The HTML is saved automatically under
`~/.context-audit/reports/` and opens in your browser. No file path or output flag
is required. Use `--client omp` (or `claude`, `codex`, `pi`) to narrow the list.
`visualize .` lists sessions whose recorded project is in the current directory
or beneath it. `--latest` skips the picker; `--no-open` skips opening the browser.

```sh
uv run --project ~/Code/context-audit context-audit sessions
uv run --project ~/Code/context-audit context-audit visualize --client omp
uv run --project ~/Code/context-audit context-audit visualize --latest --no-open
```

Session lists are readable tables by default; use `sessions --json` for scripts.

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run context-audit scan /path/to/project
uv run context-audit scan /path/to/project --format json --output before.json
uv run context-audit scan /path/to/project --card /path/to/start-card.md
```

The scan reads user and project configuration, instruction ancestors, skills,
subagent definitions, rules, commands, plugin registrations and cached manifests.
It inventories hooks without running their commands. JSON and Markdown reports
contain paths, hashes and findings, not raw instruction bodies, environment values,
hook commands or credentials. Paths, names and matchers can still be sensitive.

`--home /path/to/home` supports fixtures or a different user's configuration. With
the current home, `CODEX_HOME` is honored. No authentication files are read.

## Measure MCP tools

Export a server's `tools/list` response using an MCP client such as the official
[MCP Inspector](https://github.com/modelcontextprotocol/inspector), then:

```sh
uv run context-audit tools tools-list.json
```

Accepts a `{"tools": [...]}` object or JSON-RPC result envelope. Reports total
serialized size, per-tool size, description/schema size, missing descriptions,
undocumented parameters, repeated descriptions and broad behavioral directives.
These are review heuristics, not proof of maliciousness or poor tool quality.

Live MCP discovery is not yet implemented. Starting configured servers may execute
programs, so passive discovery and explicit connection will remain separate modes.

## Track changes

```sh
uv run context-audit scan /path/to/project --format json --output after.json
uv run context-audit diff before.json after.json
```

Snapshot IDs preserve source path, kind and component identity. Diffs show added,
removed and changed items, including token deltas. Keep snapshots in a private
directory; machine-specific paths and configuration metadata remain in them.

## Observe recorded usage

```sh
uv run context-audit observe /path/to/session.jsonl
```

Reads explicit Codex rollout or Claude Code transcript files. Codex cumulative
usage snapshots are not summed. Claude message IDs are deduplicated per file.
No conversation bodies are emitted. The report distinguishes recorded usage from
static token estimates; it cannot attribute a provider input count to individual
instruction files or hook outputs. Sessions copied to different files can overlap.

## Audit a previous session

Generate an interactive, self-contained HTML context timeline:

```sh
uv run context-audit visualize /path/to/session.jsonl --output session-map.html
open session-map.html
```

The timeline runs from oldest to newest and supports token-proportional or equal
event spacing, zoom, category filters and source totals. Select a block to inspect
its provenance, transcript line, timestamp, epoch and content hash; follow its
origin link to the tool call that produced it.
Clicking opens a detail dialog with the tool name, skill identifier, file path and
read range when those identifiers are recorded. Existing exported HTML pages must
be regenerated to include new detail fields. Categories distinguish user prompts,
agent responses, tool-call JSON, tool results, skill injections, file reads,
instructions, hook output, compaction summaries and unmeasured media.

Blocks are mutually exclusive: a skill result counts as skill text, not also as a
generic tool result. The graph shows **cumulative recorded introductions**, not an
exact reconstruction of the current context window. Compaction boundaries are
visible; retained context and missing injections remain unknown. All logged
branches are included. Hook groups without a unique owner are labeled unresolved.
The generated HTML works offline and embeds only metadata and hashes, never raw
message bodies or tool arguments. Paths and source names remain visible.

```sh
uv run context-audit sessions --client omp --limit 10
uv run context-audit session /path/to/session.jsonl
uv run context-audit session /path/to/session.jsonl --client pi
uv run context-audit session /path/to/session.jsonl --format json --output session-audit.json
```

The historical session auditor supports all four clients. It matches skill read
calls with recorded tool results and counts the returned text using the selected
encoding. It uses **the historical output**, never today's on-disk skill content.
Repeated reads count again; duplicate result records do not. Failed reads are
identified and excluded from attributed skill tokens. Skill calls, usage records,
compaction boundaries and available hook context are included in one report.
Claude's native Skill tool is linked to its separate injected skill-body message;
the launch acknowledgement is not counted as the body. Pi/OMP `skill://` reads
are recognized as well as filesystem `SKILL.md` reads.

OMP's recorded `contextSnapshot` exposes `promptTokens`, `nonMessageTokens` and
`compactionEpoch` when present. Codex per-response `token_usage_record` and older
cumulative counters are kept separate. Claude response blocks are deduplicated by
message ID, retaining the latest usage record for that response.

Direct skill reads can be attributed to their returned text. Shell commands and
code orchestration that mention skill paths may mix several outputs: these are
reported as candidates, with **unknown attribution**, rather than assigning the
entire result to a skill. Related filesystem reference reads and unlinked skill
injections are not yet attributed. A historical report audits all recorded
branches; it does not claim they all belonged to the final active context.

Pi/OMP configuration discovery includes agent settings, instruction files, skills,
prompt templates and local extension code. Extension source size is inventory
only, not prompt cost. Arbitrary extension behavior and package-installed extension
activation require further resolution or runtime evidence.

## Reversible hook changes

The scan identifies each hook by event, group index and hook index. Removal is
previewed by default. This supports JSON hook configuration, not TOML or plugin
package edits.

```sh
uv run context-audit disable-hook /path/to/settings.json SessionStart 0 0
uv run context-audit disable-hook /path/to/settings.json SessionStart 0 0 --apply
uv run context-audit restore /path/to/.context-audit/settings.json.HASH.receipt.json
```

Applying creates a private backup and receipt beside the original file, then
atomically updates it. Restore refuses to overwrite changes made after the edit.
JSON formatting is normalized. A preview contains a local configuration diff and
can contain secrets from edited context; inspect it locally before sharing.

## Measurement contract

- `o200k_base` counts actual tokens for that encoding. It is an **estimate for the
  target agent**, which may use another tokenizer or additional message framing.
- `startup-candidate` means an instruction file was found on a startup search path;
  it does not prove a particular client loaded it. Codex and Claude candidates are
  shown together and must not be treated as one client's startup total.
- Skill metadata and full skill text are separate. Full skill files are on demand.
- Hook configuration tokens are not tokens injected by the hook. Runtime output is
  marked unknown. Commands are never executed during scanning.
- MCP configured servers have unknown schema costs until schemas are supplied.
  Deferred tool loading and client filtering may reduce actual prompt cost.
- Plugin cache presence does not establish activation.
- Recorded input usage measures requests; it is not unique context accumulated.
  Caching affects billing/reuse, not the amount of text occupying context.
- No report claims to reconstruct a provider's hidden prompt or exact bill.

## Current coverage limits

Version 0.1 is useful for inventory and change tracking, but not a complete client
configuration emulator. Managed policies, profile precedence, custom instruction
fallbacks, recursive `@` imports, auto-memory, plugin enablement resolution, nested
conditional instructions, hook output capture and native tool definitions remain
work to do. Instruction assessment currently detects large/repeated text and
import references; semantic contradiction detection is not implemented.

## Development

```sh
uv run python -m unittest discover -s tests -v
uv build
```

See [the product direction](docs/product.md) for the intended full audit workflow.
