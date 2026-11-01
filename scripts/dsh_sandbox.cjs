'use strict';
// Host broker owns credentials; an offline container can request only these fixed APIs.
const fs=require('node:fs'),path=require('node:path');
const {spawn}=require('node:child_process');
const {randomUUID}=require('node:crypto');
const {openRunLog}=require('./broker_lifecycle.cjs');
const {sandboxProvider}=require('./sandbox_provider.cjs');
const {forwardProvider}=require('./batch_budget.cjs');
const {loadModelRoute,modelEndpoint}=require('./model_route.cjs');
const {prepareAssets,containerArgs}=require('./sandbox_layout.cjs');

function atomicJson(file,value) {
  const temporary=file+'.'+randomUUID()+'.tmp'; fs.writeFileSync(temporary,JSON.stringify(value),{flag:'wx'});fs.renameSync(temporary,file);
}

exports.runSandbox=async function runSandbox(cli,runtime,env,timeout,cancelFile,route) {
  const state=path.join(runtime,'sandbox');fs.mkdirSync(state,{recursive:true});
  const id=randomUUID(),run=path.join(state,'runs',id),output=path.join(run,'output');
  fs.mkdirSync(output,{recursive:true});
  const assets=prepareAssets(run);
  const input=JSON.parse(fs.readFileSync(env.BANK_V1_REQUEST,'utf8'));
  const policy=loadModelRoute();
  const provider=sandboxProvider(route);
  atomicJson(path.join(run,'request.json'),{...input,provider,selection:{provider:policy.provider,model:policy.model}});
  const scope=input.data_scope==='moneydata'?'moneydata':'bank';
  const home=path.join(state,'home-'+scope);fs.mkdirSync(home,{recursive:true});
  const allowed=new Set(input.tools.map(s=>s.name)),seen=new Set(),pending=new Set();
  const lifecycle=openRunLog(run,[...allowed]);
  let childProcess=null,buffer='',stopContainer=()=>{};
  const reply=(id,value)=>{if(childProcess?.stdin.writable)childProcess.stdin.write('BANK_REPLY '+JSON.stringify({id,...value})+'\n');};
  const abort=new AbortController(); let violations=0;
  const handle=async request=>{
    let span=null,httpStatus=null;
    try {
      if(typeof request.body!=='string'||Buffer.byteLength(request.body)>1000000)throw Error('Invalid request');
      let url,headers,body=request.body;
      if(request.method==='POST'&&request.path==='/model/chat/completions') {
        const parsed=JSON.parse(body);
        if(!Array.isArray(parsed.messages))throw Error('Invalid model request');
        if(parsed.messages.length>150||JSON.stringify(parsed.messages).length>800000)throw Error('Model input limit');
        if(parsed.tools!==undefined&&(!Array.isArray(parsed.tools)||parsed.tools.length>32||JSON.stringify(parsed.tools).length>100000))throw Error('Model tools limit');
        const permitted=new Set(['model','messages','tools','tool_choice','parallel_tool_calls','stream','stream_options','max_tokens','max_completion_tokens','temperature','top_p','reasoning_effort','thinking','reasoning']);
        if(Object.keys(parsed).some(key=>!permitted.has(key)))throw Error('Unexpected model field');
        for(const key of ['max_tokens','max_completion_tokens'])if(parsed[key]!==undefined&&(!Number.isInteger(parsed[key])||parsed[key]<1||parsed[key]>8192))throw Error('Invalid output budget');
        if(parsed.max_tokens===undefined&&parsed.max_completion_tokens===undefined)parsed.max_tokens=8192;
        body=JSON.stringify(parsed);
        url=modelEndpoint(policy,parsed.model);
        headers={'Content-Type':'application/json',Authorization:'Bearer '+env[policy.apiKeyEnv]};
      } else if(request.method==='POST'&&request.path.startsWith('/api/tools/')&&allowed.has(request.path.slice('/api/tools/'.length))) {
        JSON.parse(body);
        url=env.BANK_V1_API_BASE+request.path;
        headers={'Content-Type':'application/json',Authorization:'Bearer '+env.BANK_V1_TOOL_TOKEN};
      } else throw Error('Broker route denied');
      // Every route and body check above has passed; keep payloads out of metadata.
      span=lifecycle.log.start(request.path==='/model/chat/completions'
        ? {kind:'model'} : {kind:'tool',tool:request.path.slice('/api/tools/'.length)});
      const init={method:'POST',headers,body,redirect:'error',
        signal:AbortSignal.any([abort.signal,AbortSignal.timeout(Math.min(timeout,170000))])};
      const response=await forwardProvider({url,init,
        budgetPath:request.path==='/model/chat/completions'?env.BANK_V1_BATCH_BUDGET:null,
        requestId:request.id});
      httpStatus=response.status;
      span?.finish(httpStatus);
      let text=response.body;
      for(const secret of [env[policy.apiKeyEnv],env.BANK_V1_TOOL_TOKEN])if(secret)text=text.split(secret).join('[REDACTED]');
      reply(request.id,{status:response.status,content_type:response.contentType,body:text});
    } catch(error) {
      span?.failure(httpStatus);
      violations++;
      // No exception text: it may contain upstream headers or local paths.
      try{reply(request.id,{status:502,content_type:'application/json',body:JSON.stringify({error:{code:'SANDBOX_BROKER_FAILED',message:'受控请求被拒绝或上游未完成。'}})});}catch{}
    }
  };
  const receive=chunk=>{
    buffer+=chunk;
    if(buffer.length>1100000){stopContainer('OUTPUT_LIMIT');return;}
    let newline;
    while((newline=buffer.indexOf('\n'))>=0){
      const line=buffer.slice(0,newline);buffer=buffer.slice(newline+1);
      if(!line.startsWith('BANK_REQUEST '))continue;
      try{
        const request=JSON.parse(line.slice(13));
        if(typeof request.id!=='string'||!/^[a-f0-9-]{36}$/.test(request.id)||seen.has(request.id))throw Error('Invalid request id');
        if(seen.size>=64||pending.size>=2)throw Error('Broker request budget');
        seen.add(request.id);const work=handle(request);pending.add(work);work.finally(()=>pending.delete(work));
      }catch{violations++;}
    }
  };
  const name='codex-bank-'+id;
  const args=containerArgs({name,cli,assets,requestFile:path.join(run,'request.json'),output,home,id});
  const started=new Date().toISOString();
  const result=await new Promise(resolve=>{
    const child=spawn('docker',args,{windowsHide:true,stdio:['pipe','pipe','pipe']});
    childProcess=child;
    child.stdin.on('error',()=>{});
    let stdout='',stderr='',stopReason=null,finished=false;
    const stop=reason=>{
      if(stopReason)return;stopReason=reason;abort.abort();
      const clean=spawn('docker',['rm','-f',name],{windowsHide:true,stdio:'ignore'});
      clean.on('error',()=>child.kill());
    };
    stopContainer=stop;
    const timer=setTimeout(()=>stop('TIMEOUT'),timeout);
    const cancel=setInterval(()=>{if(cancelFile&&fs.existsSync(cancelFile))stop('CANCELLED');},200);
    const finish=(code,error)=>{
      if(finished)return;finished=true;clearTimeout(timer);clearInterval(cancel);
      resolve({code,stdout,stderr,stopReason,error,pid:child.pid});
    };
    // Decode across chunk boundaries so Chinese request text cannot split UTF-8 bytes.
    child.stdout.setEncoding('utf8'); child.stderr.setEncoding('utf8');
    child.stdout.on('data',receive);
    child.stderr.on('data',text=>{stderr+=text;if(stderr.length>1000000)stop('OUTPUT_LIMIT');});
    child.on('error',e=>finish(null,e.message));child.on('close',code=>finish(code));
  });
  abort.abort();await Promise.allSettled([...pending]);
  lifecycle.close();
  for(const [name,target] of [['trace.jsonl',env.BANK_V1_TRACE],['result.json',env.BANK_V1_RESULT]]) {
    const source=path.join(output,name);
    if(fs.existsSync(source)) {
      for(const item of [state,path.join(state,'runs'),run,output,source])if(fs.lstatSync(item).isSymbolicLink())throw Error('Unsafe sandbox output');
      const real=fs.realpathSync(source),base=fs.realpathSync(output)+path.sep,stat=fs.statSync(source);
      if(!real.startsWith(base)||!stat.isFile()||stat.size>8000000)throw Error('Invalid sandbox output');
      fs.copyFileSync(source,target);
    }
  }
  atomicJson(path.join(path.dirname(env.BANK_V1_REQUEST),'sandbox.json'),{
    backend:'docker-offline-pipes',container:name,image:'node:22-slim',started_at:started,finished_at:new Date().toISOString(),
    network:'none',user:'65534:65534',root_readonly:true,capabilities:[],no_new_privileges:true,
    assets_readonly:true,current_run_only:true,scope_session_only:true,host_credentials_mounted:false,bank_database_mounted:false,broker_requests:seen.size,broker_failures:violations,
    broker_lifecycle:{file:'broker-lifecycle.jsonl',...lifecycle.log.stats()},
    exit_code:result.code,stop_reason:result.stopReason});
  return result;
};
