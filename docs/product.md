# Product direction

Help developers answer: What have I installed? What changes the context stream?
When does it activate? What does it cost? What changed? How can I change it back?

## Evidence model

Every context source needs provenance (client, scope, source path, owning plugin),
activation conditions, a measured or unknown footprint, and an evidence level:

1. Discovered: a file/package is present.
2. Configured: a client configuration references it.
3. Resolved: client precedence and policy make it eligible to load.
4. Observed: a request or session trace demonstrates actual use.

Do not silently promote evidence between these levels. Token measurements also
need a tokenizer, serialized representation, time, and whether they are estimates
or provider/client-reported values.

## Initial implementation

- Local Codex/Claude/Pi/OMP instruction, skill, command, rule, agent and plugin inventory.
- Static hooks inventory and heuristic audit.
- MCP exported schema analysis.
- Recorded token observations from session JSONL.
- Historical skill-read attribution, compaction boundaries, recorded hook context
  and OMP context snapshots; mixed shell outputs remain explicitly ambiguous.
- JSON/Markdown reports and snapshot diffs.
- Explicit, backed-up hook edits with guarded restore.

## Next milestones

1. Resolve client-specific precedence, profiles, managed policy, plugin registries,
   skill exclusions and imports. Represent relationships between owner, source,
   trigger, generated output and consuming client. Separate client budgets.
2. Add explicit live MCP inspection with timeouts, pagination and lifecycle cleanup;
   retain schema snapshots and detect changed tool descriptions. Never run tools
   during discovery. Track deferred versus eager tool exposure.
3. Add optional runtime collection for hook frequency, injected output size and
   request composition. Correlate sources without claiming attribution from
   aggregate usage alone. Keep storage local and redact before export.
4. Introduce reviewable change plans for tools, skills, hooks and instructions,
   with backup, concurrency checks and rollback. No automatic deletion based on
   token size alone.
5. Add an interactive local UI: source inventory, activation graph, token breakdown,
   setup timeline, findings and change preview. Support watch mode and budget CI.
6. Evaluate setup variants on repeatable tasks: correctness, tool selection,
   latency and tokens. A smaller prompt is not automatically a better setup.

## References

- Claude settings: https://code.claude.com/docs/en/settings
- Claude hooks: https://code.claude.com/docs/en/hooks
- Codex instructions: https://developers.openai.com/codex/guides/agents-md
- MCP tools: https://modelcontextprotocol.io/specification/latest/server/tools

Client formats evolve. Adapter tests should pin fixture versions and report
unsupported constructs explicitly rather than silently invent runtime behavior.
