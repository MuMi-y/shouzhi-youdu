'use strict';
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {loadModelRoute,authorizedProvider,modelEndpoint} = require('../../scripts/model_route.cjs');
const policy = loadModelRoute();
const example = JSON.parse(fs.readFileSync(path.join(__dirname,'../../config/settings.example.yaml'),'utf8'));
test('project sample agrees with selected model and strips private/custom fields', () => {
  const settings = structuredClone(example);
  Object.assign(settings['llm-pi-ai'].providers[policy.provider], {apiKey:'PRIVATE',headers:{Authorization:'PRIVATE'}});
  const actual = authorizedProvider(settings,policy);
  assert.deepEqual(example['agent-default-model'],{provider:policy.provider,model:policy.model});
  assert.equal(actual.models[0].id,policy.model);
  assert(!JSON.stringify(actual).includes('PRIVATE'));
  assert.equal(actual.retryPolicy.maxRetries,0);
});
test('wrong provider endpoint, credential ref, protocol and model fail closed', () => {
  for (const delta of [{baseURL:'https://example.invalid'}, {apiKeyEnv:'OTHER_KEY'},
    {api:'openai-responses'}, {models:[{id:'kimi-k3'}]}, {models:[]}]) {
    const settings = structuredClone(example);
    Object.assign(settings['llm-pi-ai'].providers[policy.provider],delta);
    assert.throws(()=>authorizedProvider(settings,policy),{code:'MODEL_ROUTE_CHANGED'});
  }
  assert.throws(()=>authorizedProvider({'llm-pi-ai':{providers:{}}},policy));
});
test('broker accepts only policy model and builds fixed chat endpoint', () => {
  assert.equal(modelEndpoint(policy,policy.model),'https://ark.cn-beijing.volces.com/api/plan/v3/chat/completions');
  for(const model of ['kimi-k3',undefined,'https://example.invalid'])
    assert.throws(()=>modelEndpoint(policy,model));
});
