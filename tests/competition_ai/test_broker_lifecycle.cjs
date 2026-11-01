'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const {createLifecycle, openRunLog} = require('../../scripts/broker_lifecycle.cjs');
const fs = require('node:fs'), path = require('node:path');
function fixture() {
  const lines = [];
  const log = createLifecycle({allowedTools: ['accounts', 'forecast'], append: line => lines.push(line)});
  return {log, lines, rows: () => lines.map(JSON.parse)};
}
// Simulates only the already-validated dispatch boundary. No network/model/Docker.
async function dispatch(log, kind, tool, transport) {
  const span = log.start({kind, tool});
  let status = null;
  try {
    const response = await transport();
    status = response.status;
    const value = await response.read();
    span?.finish(status);
    return value;
  } catch (error) { span?.failure(status); throw error; }
}
test('model in flight is visible before cancellation; abort gets one terminal row', async () => {
  const f = fixture(), abort = new AbortController();
  const failure = new Error('SECRET-body-header-url-error');
  const run = dispatch(f.log, 'model', null, () => new Promise((resolve, reject) =>
    abort.signal.addEventListener('abort', () => reject(failure), {once: true})));
  assert.equal(f.rows()[0].event, 'start'); assert.equal(f.log.stats().active, 1);
  abort.abort(); await assert.rejects(run, error => error === failure);
  assert.deepEqual(f.rows().map(row => row.event), ['start', 'failure']);
  assert.equal(f.log.stats().active, 0);
  assert.equal(f.rows()[0].request_id, f.rows()[1].request_id);
  assert(!f.lines.join('').includes('SECRET'));
});
test('successful/HTTP-error transfers preserve content and status, not business success', async () => {
  for (const status of [200, 409, 500]) {
    const f = fixture(), secretBody = {payload: 'SECRET-consumption'};
    assert.equal(await dispatch(f.log, 'tool', 'accounts', async () => ({status, read: async () => secretBody})), secretBody);
    assert.equal(f.rows()[1].event, 'finish'); assert.equal(f.rows()[1].http_status, status);
    assert(!f.lines.join('').includes('SECRET'));
  }
});
test('body stream failure retains upstream HTTP status without error text', async () => {
  const f = fixture();
  await assert.rejects(dispatch(f.log, 'model', null, async () => ({status: 200, read: async () => {throw Error('SECRET');}})));
  assert.equal(f.rows()[1].event, 'failure'); assert.equal(f.rows()[1].http_status, 200);
  assert(!f.lines.join('').includes('SECRET'));
});
test('exact schema excludes unknown metadata; only trusted allowed tool identifiers', () => {
  const f = fixture();
  assert.equal(f.log.start({kind: 'SECRET'}), null);
  assert.equal(f.log.start({kind: 'tool', tool: 'SECRET'}), null);
  assert.equal(f.log.start({kind: 'model', tool: 'accounts'}), null);
  f.log.start({kind: 'tool', tool: 'forecast', headers: 'SECRET', body: 'SECRET', url: 'SECRET', error: 'SECRET'}).finish(200);
  for (const row of f.rows()) {
    assert.deepEqual(Object.keys(row), ['time', 'event', 'kind', 'tool', 'request_id', 'http_status']);
    assert.match(row.request_id, /^[a-f0-9-]{36}$/); assert.equal(row.tool, 'forecast');
  }
  assert(!f.lines.join('').includes('SECRET'));
});
test('hard budget 64 starts, 128 rows, duplicate terminal suppressed', () => {
  const f = fixture();
  for (let i = 0; i < 64; i++) { const span = f.log.start({kind: 'model'}); span.finish(200); span.failure(502); }
  assert.equal(f.log.start({kind: 'model'}), null); assert.equal(f.rows().length, 128);
  assert.equal(new Set(f.rows().map(row => row.request_id)).size, 64);
});
test('concurrent requests remain separately identifiable and ordered per request', () => {
  const f = fixture(), one = f.log.start({kind: 'model'}), two = f.log.start({kind: 'tool', tool: 'accounts'});
  two.finish(200); one.failure(null);
  assert.equal(f.rows()[0].request_id, f.rows()[3].request_id);
  assert.equal(f.rows()[1].request_id, f.rows()[2].request_id);
});
test('storage failure never changes dispatch result; degradation is explicit', async () => {
  const log = createLifecycle({allowedTools: [], append: () => { throw Error('SECRET-filesystem'); }});
  assert.equal(await dispatch(log, 'model', null, async () => ({status: 200, read: async () => 'normal'})), 'normal');
  assert.deepEqual(log.stats(), {requests: 1, active: 0, log_healthy: false});
});
test('abrupt kill leaves start-only as unknown, not a fabricated finish', () => {
  const f = fixture(); f.log.start({kind: 'model'});
  assert.equal(f.rows().length, 1); assert.equal(f.log.stats().active, 1);
});
test('host run file is immediately readable and exclusive; no existing log overwritten', () => {
  const run = fs.mkdtempSync(path.join(__dirname, 'offline-run-'));
  const file = path.join(run, 'broker-lifecycle.jsonl');
  const first = openRunLog(run, ['accounts']);
  try {
    const span = first.log.start({kind: 'model'});
    assert.equal(JSON.parse(fs.readFileSync(file, 'utf8')).event, 'start');
    const second = openRunLog(run, []);
    second.log.start({kind: 'model'}).finish(200);
    assert.equal(second.log.stats().log_healthy, false); second.close();
    assert.equal(fs.readFileSync(file, 'utf8').trim().split('\n').length, 1);
    span.failure(null);
    assert.equal(fs.readFileSync(file, 'utf8').trim().split('\n').length, 2);
  } finally { first.close(); fs.unlinkSync(file); fs.rmdirSync(run); }
});
