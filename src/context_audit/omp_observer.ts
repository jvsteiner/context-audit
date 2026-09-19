// Passive OMP observer. Never returns replacement payloads or hook decisions.
import { readFile } from 'node:fs/promises';
import { homedir } from 'node:os';
import { join } from 'node:path';

export default function (pi: any) {
  let warned = false;
  async function send(event: any, ctx: any, provider = false, runtime = false) {
    try {
      const sessionId = ctx.sessionManager.getSessionId();
      const config = JSON.parse(await readFile(join(homedir(), '.context-audit/service.json'), 'utf8'));
      const route = config.routes.codex;
      const body = JSON.stringify({ client: 'omp', session_id: sessionId,
        event: runtime ? 'runtime_context' : provider ? 'provider_request' : 'lifecycle', payload: provider || runtime ? event.payload : event });
      if (Buffer.byteLength(body) > 32_000_000) throw new Error('capture size limit');
      const response = await fetch(`http://127.0.0.1:${route.port}${route.prefix}/events`, {
        method: 'POST', headers: { 'Content-Type': 'application/json' }, body,
        signal: AbortSignal.timeout(2000),
      });
      if (!response.ok) throw new Error('capture unavailable');
      warned = false;
    } catch {
      if (!warned && ctx.hasUI) ctx.ui.notify('Context Audit observation unavailable; agent behavior is unchanged.', 'warning');
      warned = true;
    }
  }
  pi.on('session_start', async (event: any, ctx: any) => {
    await send(event, ctx);
    try {
      const active = new Set(pi.getActiveTools());
      const tools = pi.getAllTools().filter((tool: any) => active.has(tool.name))
        .map((tool: any) => ({name: tool.name, description: tool.description, input_schema: tool.parameters}));
      await send({payload: {system: ctx.getSystemPrompt(), tools}}, ctx, false, true);
    } catch {
      if (ctx.hasUI) ctx.ui.notify('Context Audit startup snapshot unavailable.', 'warning');
    }
  });
  for (const event of ['before_agent_start', 'tool_result', 'session_compact', 'session_shutdown']) {
    pi.on(event, async (payload: any, ctx: any) => { await send(payload, ctx); });
  }
  pi.on('before_provider_request', async (payload: any, ctx: any) => { await send(payload, ctx, true); });
}
