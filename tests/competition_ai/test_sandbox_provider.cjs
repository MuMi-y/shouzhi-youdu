'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const path = require('node:path');
const {pathToFileURL} = require('node:url');
const {createRequire} = require('node:module');
const {sandboxProvider} = require('../../scripts/sandbox_provider.cjs');
const installed = process.env.DSH_PACKAGE_PATH || path.join(process.env.APPDATA || require('node:os').homedir(), 'npm/node_modules/@deepseek-ai/dsh');
const req = createRequire(path.join(installed, 'package.json'));
const OpenAI = req('openai').OpenAI;

test('strict allowed fields: detached model data, local timeout and retry only', () => {
  const route = {api: 'openai-completions', models: [{id: 'kimi-k3'}], compat: {supportsStore: false},
    timeoutMs: 60000, retryPolicy: {mode: 'normal', maxRetries: 1},
    baseURL: 'https://example.invalid', apiKey: 'SECRET', headers: {Authorization: 'SECRET'},
    apiKeyEnv: 'OTHER_SECRET_NAME', streamIdleTimeoutMs: 1};
  const before = structuredClone(route), provider = sandboxProvider(route);
  assert.deepEqual(Object.keys(provider).sort(), ['api', 'models', 'compat', 'baseURL', 'apiKeyEnv', 'timeoutMs', 'retryPolicy'].sort());
  assert.equal(provider.timeoutMs, 180000);
  assert.deepEqual(provider.retryPolicy, {mode: 'normal', maxRetries: 0});
  assert.equal(provider.baseURL, 'http://127.0.0.1:18180/model');
  assert.equal(provider.apiKeyEnv, 'DSH_BROKER_PLACEHOLDER');
  assert(!JSON.stringify(provider).includes('SECRET'));
  provider.models[0].id = 'changed'; provider.compat.supportsStore = true;
  assert.deepEqual(route, before);
});

test('optional compatibility stays optional; outer budget is never part of provider', () => {
  const provider = sandboxProvider({api: 'openai-completions', models: []});
  assert.equal(provider.compat, undefined);
  assert(!('outerTimeoutMs' in provider));
  assert(!('streamIdleTimeoutMs' in provider));
});

test('installed DSH retry resolver accepts normal maxRetries zero', async () => {
  const {resolveRetryPolicy} = await import(pathToFileURL(req.resolve('@deepseek-ai/dsh-llm')).href);
  const policy = resolveRetryPolicy(sandboxProvider({api: 'openai-completions', models: []}).retryPolicy, 'offline-test');
  assert.equal(policy.maxRetries, 0); assert.equal(policy.mode, 'normal');
  assert(policy.retryableCodes.includes('TIMEOUT'));
});

test('installed OpenAI SDK times out before headers, without hidden SDK retries', async () => {
  let calls = 0;
  const client = new OpenAI({apiKey: 'offline-placeholder', maxRetries: 0,
    fetch: async (_url, options) => {
      calls++;
      return new Promise((_resolve, reject) => options.signal.addEventListener('abort',
        () => reject(new DOMException('Offline cancellation', 'AbortError')), {once: true}));
    }});
  const request = client.chat.completions.create({model: 'offline', messages: [], stream: true},
    {timeout: 40, maxRetries: 0});
  await assert.rejects(request, error => error instanceof req('openai').APIConnectionTimeoutError);
  assert.equal(calls, 1);
});

test('installed OpenAI SDK timeout ends at headers, not completion of SSE body', async () => {
  let calls = 0, timer;
  const client = new OpenAI({apiKey: 'offline-placeholder', maxRetries: 0,
    fetch: async () => {
      calls++;
      const body = new ReadableStream({start(controller) {
        timer = setTimeout(() => {
          controller.enqueue(new TextEncoder().encode('data: {"id":"offline","choices":[{"delta":{"content":"done"}}]}\n\ndata: [DONE]\n\n'));
          controller.close();
        }, 150);
      }, cancel() { clearTimeout(timer); }});
      return new Response(body, {status: 200, headers: {'Content-Type': 'text/event-stream'}});
    }});
  const started = performance.now();
  const stream = await client.chat.completions.create({model: 'offline', messages: [], stream: true},
    {timeout: 40, maxRetries: 0});
  const values = [];
  for await (const event of stream) values.push(event.choices[0].delta.content);
  assert.deepEqual(values, ['done']);
  assert(performance.now() - started >= 100);
  assert.equal(calls, 1);
});
