'use strict';
// Offline deployment preflight. No credentials, broker, prompt, or model call.
const fs=require('node:fs'),path=require('node:path'),crypto=require('node:crypto');
const {spawnSync}=require('node:child_process');
const {prepareAssets,containerArgs}=require('./sandbox_layout.cjs');
const {loadModelRoute}=require('./model_route.cjs');
const root=path.resolve(__dirname,'..');
const [runtimeArg,reportArg]=process.argv.slice(2);
if(!runtimeArg||!reportArg)throw Error('Usage: node scripts/check_sandbox_runtime.cjs RUNTIME_DIR REPORT_JSON');
const runtime=path.resolve(runtimeArg),reportFile=path.resolve(reportArg),id=crypto.randomUUID();
const run=path.join(runtime,'dsh','sandbox','runs',id),output=path.join(run,'output');
fs.mkdirSync(output,{recursive:true});
const assets=prepareAssets(run),home=path.join(runtime,'dsh','sandbox','home-bank-preflight');
fs.mkdirSync(home,{recursive:true});
const sha=data=>crypto.createHash('sha256').update(data).digest('hex');
const manifest=Object.fromEntries(fs.readdirSync(assets).map(name=>[name,sha(fs.readFileSync(path.join(assets,name)))]));
const route=loadModelRoute(),selection={provider:route.provider,model:route.model};
const requestFile=path.join(run,'request.json');
fs.writeFileSync(requestFile,JSON.stringify({mode:'OFFLINE_PREFLIGHT_NO_MODEL',manifest,selection}));
const cli=path.join(root,'node-runtime/node_modules/@deepseek-ai/dsh');
const name='youshu-preflight-'+id;
const args=containerArgs({name,cli,assets,requestFile,output,home,id});
const program=String.raw`
(async()=>{
  const fs=require('node:fs'),crypto=require('node:crypto'),vm=require('node:vm');
  const {createRequire}=require('node:module'),{pathToFileURL}=require('node:url');
  const req=createRequire('/opt/dsh/package.json'),input=JSON.parse(fs.readFileSync('/request.json','utf8'));
  const result={mode:input.mode,provider_requests:0,assets:{},dependencies:{},writes:[],readonly_denied:[]};
  const assert=(ok,message)=>{if(!ok)throw Error(message)};
  for(const [name,expected] of Object.entries(input.manifest)){
    const bytes=fs.readFileSync('/assets/'+name),hash=crypto.createHash('sha256').update(bytes).digest('hex');
    assert(hash===expected,'ASSET_HASH_MISMATCH:'+name);
    if(name.endsWith('.cjs'))new vm.Script(bytes.toString('utf8'),{filename:name});
    result.assets[name]=hash;
  }
  const pkg=JSON.parse(fs.readFileSync('/opt/dsh/package.json','utf8'));
  assert(pkg.version==='0.1.1-rc.2','DSH_VERSION_MISMATCH'); result.dsh_version=pkg.version;
  const load=async name=>{const file=req.resolve(name);fs.readFileSync(file);await import(pathToFileURL(file).href);result.dependencies[name]=file;};
  for(const name of ['@deepseek-ai/dsh-app-boot','@deepseek-ai/dsh-launch-environment','@deepseek-ai/dsh-agent',
      '@deepseek-ai/dsh-llm','@deepseek-ai/dsh-scope','@deepseek-ai/dsh-persona','@deepseek-ai/dsh-agent-presets'])await load(name);
  const {loadOverlayPatches}=await import(pathToFileURL(req.resolve('@deepseek-ai/dsh-app-boot')).href);
  const baseFile=req.resolve('@deepseek-ai/dsh-base/cordis.patch.yml');
  const rows=loadOverlayPatches('bank-v1',baseFile);
  const walk=rows=>{for(const row of rows){if(row.name&&!row.name.startsWith('file:')){
    const resolved=req.resolve(row.name);fs.readFileSync(resolved);result.dependencies[row.name]=resolved;
  }if(Array.isArray(row.insert))walk(row.insert);if(Array.isArray(row.config))walk(row.config);}};
  walk(rows); result.base_config=baseFile;
  for(const dir of ['/session/profiles/bank-v1','/session/agent-presets/bank-v1'])fs.mkdirSync(dir,{recursive:true});
  const configs={'/session/settings.json':{'agent-default-model':input.selection},
    '/session/profiles/bank-v1/cordis.patch.yml':[], '/session/agent-presets/bank-v1/agent.cordis.yml':[]};
  for(const [file,value] of Object.entries(configs)){fs.writeFileSync(file,JSON.stringify(value));JSON.parse(fs.readFileSync(file,'utf8'));result.writes.push(file);}
  for(const file of ['/output/probe.json','/tmp/probe.json']){fs.writeFileSync(file,'{}');assert(fs.readFileSync(file,'utf8')==='{}','WRITE_READ_FAILED');result.writes.push(file);}
  for(const dir of ['/assets','/opt/dsh','/opt/node_modules','/']){
    const file=dir+'/.youshu-preflight-write'; let denied=false;
    try{fs.writeFileSync(file,'probe',{flag:'wx'});fs.unlinkSync(file);}catch(e){denied=['EROFS','EACCES','EPERM'].includes(e.code);}
    assert(denied,'READONLY_NOT_ENFORCED:'+dir);result.readonly_denied.push(dir);
  }
  result.uid=process.getuid();assert(result.uid===65534,'UID_CHANGED');
  result.net_interfaces=fs.readdirSync('/sys/class/net');assert(result.net_interfaces.length===1&&result.net_interfaces[0]==='lo','NETWORK_CHANGED');
  const status=fs.readFileSync('/proc/self/status','utf8');assert(/CapEff:\s+0+\n/.test(status),'CAPABILITIES_CHANGED');
  assert(/NoNewPrivs:\s+1/.test(status),'NO_NEW_PRIVILEGES_CHANGED');
  result.ok=true;fs.writeFileSync('/output/preflight.json',JSON.stringify(result));
  console.log('PREFLIGHT '+JSON.stringify(result));
})().catch(e=>{console.error(e.stack);process.exitCode=1;});`;
args.splice(args.length-2,2,'-e',program);
const started=new Date().toISOString();
const child=spawnSync('docker',args,{encoding:'utf8',timeout:60000,maxBuffer:2000000,windowsHide:true});
if(child.error)spawnSync('docker',['rm','-f',name],{encoding:'utf8',timeout:10000,windowsHide:true});
const resultFile=path.join(output,'preflight.json');
const report={started_at:started,finished_at:new Date().toISOString(),runtime,run,selection,
  provider_requests:0,credentials_read:false,broker_started:false,model_started:false,
  exit_code:child.status,error:child.error?.code||null,stderr:child.stderr,
  result:fs.existsSync(resultFile)?JSON.parse(fs.readFileSync(resultFile,'utf8')):null};
report.ok=child.status===0&&report.result?.ok===true;
fs.mkdirSync(path.dirname(reportFile),{recursive:true});fs.writeFileSync(reportFile,JSON.stringify(report,null,2));
console.log(JSON.stringify({ok:report.ok,report:reportFile,provider_requests:0,error:report.error,exit_code:child.status}));
process.exitCode=report.ok?0:1;
