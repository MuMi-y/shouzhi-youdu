'use strict';
// Host-only request metadata. Never records bodies, credentials, URLs or raw errors.
const fs = require('node:fs');
const path = require('node:path');
const {randomUUID} = require('node:crypto');

function createLifecycle({allowedTools, append, now = () => new Date(), uuid = randomUUID}) {
  const allowed = new Set(allowedTools.filter(name =>
    typeof name === 'string' && /^[a-z][a-z0-9_]{0,63}$/.test(name)));
  let requests = 0, active = 0, healthy = true;
  function write(event, descriptor, httpStatus) {
    if (!healthy) return;
    try {
      const status = Number.isInteger(httpStatus) && httpStatus >= 100 && httpStatus <= 599 ? httpStatus : null;
      // Construct an exact allowlist; never spread request/response/error objects.
      const row = {time: now().toISOString(), event, kind: descriptor.kind,
        tool: descriptor.tool, request_id: descriptor.id, http_status: status};
      append(JSON.stringify(row) + '\n');
    } catch { healthy = false; }
  }
  function start({kind, tool = null}) {
    if (requests >= 64 || (kind !== 'model' && kind !== 'tool') ||
        (kind === 'model' && tool !== null) || (kind === 'tool' && !allowed.has(tool))) return null;
    // Host-generated UUID, not the container's loosely checked request id.
    let id;
    try { id = uuid(); } catch { healthy = false; return null; }
    if (typeof id !== 'string' || !/^[a-f0-9]{8}-[a-f0-9]{4}-4[a-f0-9]{3}-[89ab][a-f0-9]{3}-[a-f0-9]{12}$/.test(id)) return null;
    const descriptor = {kind, tool, id};
    requests++; active++;
    write('start', descriptor, null);
    let ended = false;
    function end(event, status) {
      if (ended) return;
      ended = true; active--;
      write(event, descriptor, status);
    }
    return Object.freeze({finish: status => end('finish', status), failure: status => end('failure', status)});
  }
  return Object.freeze({start, stats: () => ({requests, active, log_healthy: healthy})});
}

function openRunLog(runDirectory, allowedTools) {
  // Caller owns a fresh host-only run directory. Do not mount this file/container root.
  const file = path.join(runDirectory, 'broker-lifecycle.jsonl');
  let fd = null;
  try { fd = fs.openSync(file, 'wx', 0o600); } catch {}
  const log = createLifecycle({allowedTools, append: line => {
    if (fd === null) throw new Error('LOG_UNAVAILABLE');
    const bytes = Buffer.from(line, 'utf8');
    if (fs.writeSync(fd, bytes) !== bytes.length) throw new Error('LOG_INCOMPLETE');
  }});
  return {log, close: () => { if (fd !== null) { try { fs.closeSync(fd); } catch {} fd = null; } }};
}
module.exports = {createLifecycle, openRunLog};
