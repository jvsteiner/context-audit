import { test, expect, mock } from 'bun:test';
import observer from '../src/context_audit/omp_observer';

test('OMP callbacks do not replace or mutate native payloads', async () => {
  const callbacks = new Map<string, Function>();
  observer({on: (name: string, callback: Function) => callbacks.set(name, callback),
    getActiveTools: () => ['read'], getAllTools: () => [{name: 'read', description: 'read', parameters: {type: 'object'}}]});
  const notifications: string[] = [];
  const ctx = {sessionManager: {getSessionId: () => 'test-only'}, getSystemPrompt: () => ['startup instructions'], hasUI: true,
    ui: {notify: (message: string) => notifications.push(message)}};
  // Intercept before fetch: the test must never contact the live recorder.
  const savedFetch = globalThis.fetch;
  globalThis.fetch = mock(async () => { throw new Error('offline'); }) as any;
  try {
    for (const [name, callback] of callbacks) {
      const payload = {type: name, payload: {messages: [{role: 'user', content: 'test'}]},
        systemPrompt: ['test'], toolName: 'read', content: [{type: 'text', text: 'test'}]};
      const original = JSON.stringify(payload);
      expect(await callback(payload, ctx)).toBeUndefined();
      expect(JSON.stringify(payload)).toBe(original);
    }
    expect(callbacks.has('before_provider_request')).toBe(true);
    expect(notifications.length).toBe(1);
  } finally { globalThis.fetch = savedFetch; }
});
