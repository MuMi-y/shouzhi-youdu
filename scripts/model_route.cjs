'use strict';
// Public project policy. Never accept an endpoint/model from conversation input.
const fs = require('node:fs');
const path = require('node:path');
function loadModelRoute() {
  return JSON.parse(fs.readFileSync(path.join(__dirname, '../config/model_route.json'), 'utf8'));
}
function authorizedProvider(settings, policy) {
  const route = settings['llm-pi-ai']?.providers?.[policy.provider];
  if (!route || route.api !== 'openai-completions' || route.baseURL !== policy.baseURL ||
      route.apiKeyEnv !== policy.apiKeyEnv || !Array.isArray(route.models) ||
      route.models.length !== 1 || route.models[0].id !== policy.model) {
    throw Object.assign(new Error('Authorized project model route unavailable'), {code: 'MODEL_ROUTE_CHANGED'});
  }
  // No custom headers or inline credentials can enter generated profiles.
  return {api: route.api, baseURL: policy.baseURL, apiKeyEnv: policy.apiKeyEnv,
    models: structuredClone(route.models), compat: structuredClone(route.compat),
    retryPolicy: {mode: 'normal', maxRetries: 0}, timeoutMs: 180000};
}
function modelEndpoint(policy, model) {
  if (model !== policy.model) throw new Error('Invalid model request');
  return policy.baseURL + '/chat/completions';
}
module.exports = {loadModelRoute, authorizedProvider, modelEndpoint};
