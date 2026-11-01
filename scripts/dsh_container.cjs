'use strict';
// Runs inside the offline container. HTTP is loopback-only; mailbox contains no credentials.
const fs=require('node:fs'),path=require('node:path'),http=require('node:http');
const {randomUUID}=require('node:crypto'),{createRequire}=require('node:module'),{pathToFileURL}=require('node:url');
const id=process.argv[2];
if(!/^[a-f0-9-]{36}$/.test(id))throw Error('Invalid run identifier');
const run='/output',req=createRequire('/opt/dsh/package.json');
const waiters=new Map();
const lines=require('node:readline').createInterface({input:process.stdin});
lines.on('line',line=>{if(!line.startsWith('BANK_REPLY '))return;try{const value=JSON.parse(line.slice(11));waiters.get(value.id)?.(value);waiters.delete(value.id);}catch{}});
const input=JSON.parse(fs.readFileSync('/request.json','utf8'));
const scope=input.data_scope==='moneydata'?'moneydata':'bank',home='/session';
const profile=home+'/profiles/bank-v1',presets=home+'/agent-presets',preset=presets+'/bank-v1';
for(const dir of [profile,preset])fs.mkdirSync(dir,{recursive:true});
function json(file,value){fs.writeFileSync(file,JSON.stringify(value));}
const server=http.createServer(async(request,response)=>{
  try{
    const chunks=[];let size=0;
    for await(const chunk of request){size+=chunk.length;if(size>1000000)throw Error('Input too large');chunks.push(chunk);}
    const requestId=randomUUID();
    const value=await new Promise((resolve,reject)=>{
      const timer=setTimeout(()=>{waiters.delete(requestId);reject(Error('Broker timeout'));},175000);
      waiters.set(requestId,value=>{clearTimeout(timer);resolve(value);});
      process.stdout.write('BANK_REQUEST '+JSON.stringify({id:requestId,method:request.method,path:request.url,body:Buffer.concat(chunks).toString('utf8')})+'\n');
    });
    response.writeHead(value.status,{'Content-Type':value.content_type});response.end(value.body);
  }catch{response.writeHead(502,{'Content-Type':'application/json'});response.end('{"error":{"message":"Local broker failed"}}');}
});
server.listen(18180,'127.0.0.1',()=>{
  const base=fs.readFileSync(req.resolve('@deepseek-ai/dsh-base/cordis.patch.yml'),'utf8');
  const keep=new Set(['timer','llm','session','agent','agent-default-model','llm-retry','settings','credentials','llm-pi-ai','session-persistence-jsonl','tools','system-prompt','agent-loop','timeout-policy','session-checkpoint-policy']);
  const ids=[...base.matchAll(/^    - id: ([\w-]+)$/gm)].map(m=>m[1]);
  const patch=ids.filter(x=>!keep.has(x)).map(x=>({id:x,disabled:true}));
  patch.push({id:'tools',config:{mode:'native'}},{id:'agent-loop',config:{agents:[],maxParallelToolCalls:1}},
    {id:'system-prompt',config:{persona:''}},{id:'settings',config:{path:home+'/settings.json',watch:false}},
    {id:'credentials',config:{path:home+'/.credentials.yaml',watch:false}},
    {insert:[{id:'bank-presets',name:'@deepseek-ai/dsh-agent-presets',config:{default:'bank-v1',roots:[{path:presets,trust:'system'}],includeUserRoot:false}},
      {id:'bank-driver',name:pathToFileURL('/assets/driver.cjs').href}]});
  json(profile+'/package.json',{name:'competition-offline-profile',private:true,dsh:{profile:{bundles:['@deepseek-ai/dsh-base']}}});
  json(profile+'/cordis.patch.yml',patch);
  json(home+'/settings.json',{'agent-default-model':input.selection,
    'llm-pi-ai':{providers:{[input.selection.provider]:input.provider}}});
  json(preset+'/agent.cordis.yml',[
    {id:'bank-persona',name:pathToFileURL(req.resolve('@deepseek-ai/dsh-persona')).href,config:{text:fs.readFileSync('/assets/'+(scope==='moneydata'?'moneydata_persona.txt':'persona.txt'),'utf8')}},
    {id:'bank-tools',name:pathToFileURL('/assets/tools.cjs').href}]);
  fs.writeFileSync(preset+'/preset.yml','name: 有数受限银行助手\ndescription: 固定银行工具\norder: 1\n');
  Object.assign(process.env,{DSH_HOME:home,BANK_V1_DSH_PACKAGE:'/opt/dsh',BANK_V1_API_BASE:'http://127.0.0.1:18180',
    BANK_V1_TOOL_TOKEN:'offline-broker-placeholder',DSH_BROKER_PLACEHOLDER:'offline-broker-placeholder',
    BANK_V1_REQUEST:'/request.json',BANK_V1_TRACE:run+'/trace.jsonl',BANK_V1_RESULT:run+'/result.json'});
  process.chdir('/session');
  require('/assets/boot.cjs');
  // The existing boot lifecycle sets exitCode; the local gateway must not keep it alive.
  const finish=setInterval(()=>{if(process.exitCode!==undefined){clearInterval(finish);server.close();lines.close();process.stdin.destroy();}},100);
});
