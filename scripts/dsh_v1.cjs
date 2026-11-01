'use strict';
// Thin process adapter: all planning/tool iteration remains in the installed DSH Agent.
const fs=require('node:fs');
const path=require('node:path');
const {randomUUID}=require('node:crypto');
const {spawn}=require('node:child_process');
const {createRequire}=require('node:module');
const {pathToFileURL}=require('node:url');
const {loadModelRoute,authorizedProvider}=require('./model_route.cjs');

function writeJson(filename,value) {fs.writeFileSync(filename,JSON.stringify(value,null,2),'utf8');}
function fail(code,message) {const error=new Error(message);error.code=code;throw error;}
function acquireRuntime(runtime) {
  fs.mkdirSync(runtime,{recursive:true});
  const lock=path.join(runtime,'.bridge-lock');
  if(fs.existsSync(lock)) {
    const previous=fs.readFileSync(lock,'utf8');
    if(/^\d+$/.test(previous)) {
      let alive=true;try{process.kill(Number(previous),0);}catch(error){if(error.code==='ESRCH')alive=false;}
      if(!alive&&fs.readFileSync(lock,'utf8')===previous) fs.unlinkSync(lock);
    }
  }
  try {const descriptor=fs.openSync(lock,'wx');fs.writeFileSync(descriptor,String(process.pid));fs.closeSync(descriptor);}
  catch(error) {
    if(error.code==='EEXIST') fail('RUNTIME_BUSY','This DSH runtime has an active or unclean bridge lock; wait for current job, or inspect the recorded PID before removing a stale lock');
    throw error;
  }
  const release=()=>{try{if(fs.readFileSync(lock,'utf8')===String(process.pid))fs.unlinkSync(lock);}catch{}};
  process.once('exit',release);
}
function redact(text,secrets) {
  for(const secret of secrets) if(secret && secret.length>5) text=text.split(secret).join('[REDACTED]');
  return text.replace(/(?:ark|sk)-[a-zA-Z0-9_-]{16,}/g,'[REDACTED]');
}
async function readInput() {
  const chunks=[];let size=0;
  for await(const chunk of process.stdin) {size+=chunk.length;if(size>100000) fail('INPUT_LIMIT','Input exceeds 100KB');chunks.push(chunk);}
  const input=JSON.parse(Buffer.concat(chunks).toString('utf8').replace(/^\uFEFF/,''));
  if(typeof input.prompt!=='string'||!input.prompt.trim()||input.prompt.length>24000) fail('INVALID_INPUT','prompt must contain 1–24000 characters');
  if(Object.keys(input).some(k=>!['prompt','session_id'].includes(k))) fail('INVALID_INPUT','Only prompt and session_id are accepted');
  if(input.session_id&&!/^bank-[a-f0-9-]{36}$/.test(input.session_id)) fail('INVALID_SESSION','Invalid session id');
  return input;
}
function localBase(value) {
  const url=new URL(value);
  if(url.protocol!=='http:'||url.hostname!=='127.0.0.1'||url.username||url.password||url.pathname!=='/'||url.search||url.hash) fail('INVALID_BANK_URL','Bank endpoint must be an explicit http://127.0.0.1:port origin');
  return url.origin;
}
async function schema(base,token) {
  const response=await fetch(base+'/api/tool-schema',{redirect:'error',headers:{Authorization:'Bearer '+token},signal:AbortSignal.timeout(10000)});
  if(!response.ok) fail('SCHEMA_FAILED','Bank tool discovery HTTP '+response.status);
  const raw=await response.text();if(raw.length>150000) fail('SCHEMA_FAILED','Schema too large');
  const catalog=JSON.parse(raw),specs=catalog.tools;
  if(!Array.isArray(specs)||!specs.length) fail('SCHEMA_FAILED','Invalid tool catalog');
  const names=new Set();
  // Explicit positive list: future write tools must not enter a repair/event turn.
  const readOnlyBankTools=new Set(['accounts','transactions','spending_summary','arrangements','forecast','operation','event_context','billing_report','recurring_candidates','insight_status','task_status','money_calculate','goal_context','compare_goal_options','preview_goal_options','compare_goal_impact','explain_goal_changes']);
  const scope=process.env.BANK_V1_DATA_SCOPE||'bank';
  if(!['bank','moneydata'].includes(scope)||(catalog.scope||'bank')!==scope) fail('DATA_SCOPE_MISMATCH','Tool credential scope does not match requested workspace');
  const allowed=new Set(scope==='moneydata'
    ? ['moneydata_overview','moneydata_transactions','moneydata_summary','moneydata_forecast']
    : ['accounts','transactions','spending_summary','arrangements','forecast','propose_arrangement','prepare_transfer','prepare_mandate_query','match_mandate_from_query','prepare_mandate_cancel','operation','event_context','billing_report','recurring_candidates','insight_status','check_insights','submit_task_plan','task_status','replan_task','money_calculate','goal_context','compare_goal_options','propose_goal','preview_goal_options','compare_goal_impact','explain_goal_changes']);
  if(specs.length>allowed.size) fail('SCHEMA_FAILED','Invalid tool catalog');
  for(const spec of specs) {
    if(!allowed.has(spec.name)||names.has(spec.name)) fail('SCHEMA_FORBIDDEN','Forbidden or duplicate tool '+spec.name);
    if(typeof spec.description!=='string'||spec.parameters?.type!=='object') fail('SCHEMA_FAILED','Invalid tool specification');
    names.add(spec.name);
  }
  return {scope,business_date:/^\d{4}-\d{2}-\d{2}$/.test(catalog.business_date)?catalog.business_date:null,
    tools:specs.filter(spec=>process.env.BANK_V1_READONLY!=='1'||scope==='moneydata'||readOnlyBankTools.has(spec.name)).map(({name,description,parameters})=>({name,description,parameters}))};
}
function prepareProfile(runtime,cli,yaml,settings) {
  const home=path.join(runtime,'home');
  const profile=path.join(home,'profiles','bank-v1');
  const presets=path.join(home,'agent-presets');
  const preset=path.join(presets,'bank-v1');
  for(const dir of [profile,preset,path.join(runtime,'runs')]) fs.mkdirSync(dir,{recursive:true});
  writeJson(path.join(profile,'package.json'),{name:'fintech-bank-v1-profile',private:true,dsh:{profile:{bundles:['@deepseek-ai/dsh-base']}}});
  const plugin=path.resolve(__dirname,'../plugins/bank-v1');
  if(!fs.existsSync(path.join(cli,'package.json'))) fail('DSH_MISSING','Run npm ci --prefix node-runtime --ignore-scripts');
  const req=createRequire(path.join(cli,'package.json'));
  const personaFile=process.env.BANK_V1_DATA_SCOPE==='moneydata'?'moneydata_persona.txt':'persona.txt';
  const persona=fs.readFileSync(path.join(plugin,personaFile),'utf8');
  writeJson(path.join(preset,'agent.cordis.yml'),[
    {id:'bank-persona',name:pathToFileURL(req.resolve('@deepseek-ai/dsh-persona')).href,config:{text:persona}},
    {id:'bank-tools',name:pathToFileURL(path.join(plugin,'tools.cjs')).href}
  ]);
  fs.writeFileSync(path.join(preset,'preset.yml'),'name: 用钱安排助手\ndescription: 受限银行工具、财务分析与持续跟进\norder: 1\n');
  const base=fs.readFileSync(req.resolve('@deepseek-ai/dsh-base/cordis.patch.yml'),'utf8');
  const keep=new Set(['timer','llm','session','agent','agent-default-model','llm-retry','settings','credentials','llm-pi-ai','session-persistence-jsonl','tools','system-prompt','agent-loop','timeout-policy','session-checkpoint-policy']);
  const ids=[...base.matchAll(/^    - id: ([\w-]+)$/gm)].map(m=>m[1]);
  const patch=ids.filter(id=>!keep.has(id)).map(id=>({id,disabled:true}));
  patch.push({id:'tools',config:{mode:'native'}},{id:'agent-loop',config:{agents:[],maxParallelToolCalls:1}});
  patch.push({id:'system-prompt',config:{persona:''}});
  patch.push({id:'settings',config:{path:path.join(home,'settings.json'),watch:false}});
  // The credential plugin sees only the explicitly inherited model key; no user-home credential file is mounted.
  patch.push({id:'credentials',config:{path:path.join(home,'.credentials.yaml'),watch:false}});
  patch.push({insert:[
    {id:'bank-presets',name:'@deepseek-ai/dsh-agent-presets',config:{default:'bank-v1',roots:[{path:presets,trust:'system'}],includeUserRoot:false}},
    {id:'bank-driver',name:pathToFileURL(path.join(plugin,'driver.cjs')).href}
  ]});
  writeJson(path.join(profile,'cordis.patch.yml'),patch);
  writeJson(path.join(home,'settings.json'),settings);
  return home;
}
function runChild(cli,home,runtime,env,timeout,cancelFile) {
  return new Promise(resolve=>{
    const child=spawn(process.execPath,[path.resolve(__dirname,'../plugins/bank-v1/boot.cjs')],{cwd:runtime,env,windowsHide:true,stdio:['ignore','pipe','pipe']});
    child.stdout.setEncoding('utf8');child.stderr.setEncoding('utf8');
    let stdout='',stderr='',stopping=false,stopReason=null;
    const stop=reason=>{
      if(stopping) return;stopping=true;stopReason=reason;
      if(process.platform==='win32'&&child.pid) {
        const killer=spawn(path.join(process.env.SystemRoot,'System32/taskkill.exe'),['/PID',String(child.pid),'/T','/F'],{windowsHide:true,stdio:'ignore'});
        killer.on('error',()=>child.kill());
      } else child.kill('SIGKILL');
    };
    const timer=setTimeout(()=>stop('TIMEOUT'),timeout);
    const poll=cancelFile?setInterval(()=>{if(fs.existsSync(cancelFile)) stop('CANCELLED');},200):null;
    const onSignal=()=>stop('CANCELLED');process.once('SIGINT',onSignal);process.once('SIGTERM',onSignal);
    const finish=(code,error)=>{
      clearTimeout(timer);if(poll)clearInterval(poll);
      process.removeListener('SIGINT',onSignal);process.removeListener('SIGTERM',onSignal);
      resolve({code,stdout,stderr,stopReason,error,pid:child.pid});
    };
    child.stdout.on('data',value=>{stdout+=value;if(stdout.length>1000000)stop('OUTPUT_LIMIT');});
    child.stderr.on('data',value=>{stderr+=value;if(stderr.length>1000000)stop('OUTPUT_LIMIT');});
    child.on('error',error=>finish(null,error.message));
    child.on('close',code=>finish(code));
  });
}
async function main() {
  const input=await readInput();
  const batchBudget=process.env.BANK_V1_BATCH_BUDGET;
  const arg=process.argv.indexOf('--runtime');
  if(arg<0||!process.argv[arg+1]) fail('INVALID_INPUT','--runtime is required');
  const runtime=path.resolve(process.argv[arg+1]);
  acquireRuntime(runtime);
  const base=localBase(process.env.BANK_V1_API_BASE);
  const token=process.env.BANK_V1_TOOL_TOKEN;
  if(!token||token.length<16) fail('MISSING_TOOL_TOKEN','Bank tool token missing');
  const cli=process.env.DSH_PACKAGE_PATH ? path.resolve(process.env.DSH_PACKAGE_PATH)
    : path.resolve(__dirname,'../node-runtime/node_modules/@deepseek-ai/dsh');
  if(!fs.existsSync(path.join(cli,'package.json'))) fail('DSH_MISSING','Run npm ci --prefix node-runtime --ignore-scripts');
  const req=createRequire(path.join(cli,'package.json'));
  if(req('./package.json').version!=='0.1.1-rc.2') fail('DSH_VERSION','Validated DSH version is 0.1.1-rc.2');
  const yaml=req('js-yaml');
  const originalHome=process.env.DSH_CONFIG_HOME ? path.resolve(process.env.DSH_CONFIG_HOME)
    : path.join(process.env.USERPROFILE||require('node:os').homedir(),'.dsh');
  // Public project model settings are independent of global DSH migrations.
  const settingsFile=process.env.DSH_CONFIG_HOME ? path.join(originalHome,'settings.yaml')
    : path.resolve(__dirname,'../config/settings.example.yaml');
  const original=yaml.load(fs.readFileSync(settingsFile,'utf8'));
  const policy=loadModelRoute();
  const route=authorizedProvider(original,policy);
  const credentialFile=path.join(originalHome,'.credentials.yaml');
  const credentials=process.env[policy.apiKeyEnv] ? null
    : (fs.existsSync(credentialFile) ? yaml.load(fs.readFileSync(credentialFile,'utf8')) : null);
  const key=process.env[policy.apiKeyEnv]||credentials?.refs?.[route.apiKeyEnv];
  if(typeof key!=='string'||!key) fail('MODEL_CREDENTIAL_MISSING','Authorized model credential unavailable');
  // Reject local dependency/configuration failures before consuming run allowance.
  if(batchBudget){
    if(process.env.BANK_V1_SANDBOX!=='1')fail('BUDGET_REQUIRES_SANDBOX','Budgeted calls require host broker');
    require('./batch_budget.cjs').reserve(batchBudget,'dsh');
  }
  const secrets=[key,token];
  const catalog=await schema(base,token),tools=catalog.tools;
  const home=prepareProfile(runtime,cli,yaml,{'agent-default-model':{provider:policy.provider,model:policy.model},'llm-pi-ai':{providers:{[policy.provider]:route}}});
  const sessionId=input.session_id||'bank-'+randomUUID();
  const run=path.join(runtime,'runs',randomUUID());fs.mkdirSync(run);
  const files={request:path.join(run,'request.json'),trace:path.join(run,'trace.jsonl'),result:path.join(run,'result.json')};
  writeJson(files.request,{...input,session_id:sessionId,resume:!!input.session_id,tools,business_date:catalog.business_date,data_scope:catalog.scope});
  const env={};
  for(const name of ['SystemRoot','WINDIR','COMSPEC','TEMP','TMP','PATH','USERPROFILE','APPDATA','LOCALAPPDATA']) if(process.env[name])env[name]=process.env[name];
  Object.assign(env,{DSH_HOME:home,DSH_TELEMETRY_DISABLED:'1',[policy.apiKeyEnv]:key,BANK_V1_API_BASE:base,BANK_V1_TOOL_TOKEN:token,BANK_V1_DSH_PACKAGE:cli,BANK_V1_REQUEST:files.request,BANK_V1_TRACE:files.trace,BANK_V1_RESULT:files.result});
  if(batchBudget)env.BANK_V1_BATCH_BUDGET=batchBudget;
  const timeout=Number(process.env.BANK_V1_TIMEOUT_MS||180000);
  if(!Number.isInteger(timeout)||timeout<1000||timeout>300000) fail('INVALID_TIMEOUT','Timeout must be 1000–300000ms');
  const started=new Date().toISOString();
  const child=process.env.BANK_V1_SANDBOX==='1'
    ? await require('./dsh_sandbox.cjs').runSandbox(cli,runtime,env,timeout,process.env.BANK_V1_CANCEL_FILE,route)
    : await runChild(cli,home,runtime,env,timeout,process.env.BANK_V1_CANCEL_FILE);
  const trace=fs.existsSync(files.trace)?fs.readFileSync(files.trace,'utf8').trim().split('\n').filter(Boolean).map(line=>JSON.parse(line)):[];
  let result={ok:false,text:'',session_id:sessionId,error:{code:child.stopReason||'DSH_PROCESS_FAILED',message:'DSH did not return a completed turn'}};
  if(!child.stopReason&&fs.existsSync(files.result)) result=JSON.parse(fs.readFileSync(files.result,'utf8'));
  result={...result,session_id:sessionId,trace,started_at:started,finished_at:new Date().toISOString(),exit_code:child.code,subprocess_id:child.pid,diagnostic:child.stderr.slice(-8000)};
  result=JSON.parse(redact(JSON.stringify(result),secrets));
  writeJson(path.join(run,'outcome.json'),result);
  process.stdout.write(JSON.stringify(result)+'\n');process.exitCode=result.ok?0:1;
}
module.exports={schema};
if(require.main===module)main().catch(error=>{process.stdout.write(JSON.stringify({ok:false,text:'',error:{code:error.code||'BRIDGE_FAILED',message:String(error.message).replace(/(?:ark|sk)-[a-zA-Z0-9_-]{16,}/g,'[REDACTED]').slice(0,300)}})+'\n');process.exitCode=1;});
