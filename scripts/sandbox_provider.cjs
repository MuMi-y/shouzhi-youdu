'use strict';
// The broker buffers a complete response (170s cap); the container waits 175s.
// Wait beyond both here, without retrying an upstream request still in flight.
// The outer per-turn deadline and cancellation still take priority.
function sandboxProvider(route) {
  return {
    api: route.api,
    models: structuredClone(route.models),
    compat: structuredClone(route.compat),
    baseURL: 'http://127.0.0.1:18180/model',
    apiKeyEnv: 'DSH_BROKER_PLACEHOLDER',
    timeoutMs: 180000,
    retryPolicy: {mode: 'normal', maxRetries: 0}
  };
}
module.exports = {sandboxProvider};
