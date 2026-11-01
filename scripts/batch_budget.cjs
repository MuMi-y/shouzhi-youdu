'use strict';
// Host-only persistent limits. Neither credential values nor prompt bodies belong here.
const fs=require('node:fs');
const {DatabaseSync}=require('node:sqlite');
const {randomUUID}=require('node:crypto');

function error(code){return Object.assign(new Error(code),{code});}
function connect(file){
  if(!fs.existsSync(file))throw error('BATCH_BUDGET_MISSING');
  const db=new DatabaseSync(file);db.exec('PRAGMA busy_timeout=10000');return db;
}
function createBudget(file,{providerLimit,dshLimit,deadlineMs,batchId=randomUUID()},now=Date.now()){
  if(!Number.isInteger(providerLimit)||providerLimit<1||providerLimit>400||
     !Number.isInteger(dshLimit)||dshLimit<1||dshLimit>230||
     !Number.isSafeInteger(deadlineMs)||deadlineMs<=now||deadlineMs>now+3*60*60*1000)
    throw error('INVALID_BATCH_LIMITS');
  const descriptor=fs.openSync(file,'wx');fs.closeSync(descriptor);
  const db=connect(file);
  try{
    db.exec(`CREATE TABLE batch(id TEXT PRIMARY KEY,provider_limit INTEGER NOT NULL,dsh_limit INTEGER NOT NULL,
      deadline_ms INTEGER NOT NULL,provider_used INTEGER NOT NULL,dsh_used INTEGER NOT NULL,
      state TEXT NOT NULL,stop_reason TEXT);
      CREATE TABLE attempts(id TEXT PRIMARY KEY,kind TEXT NOT NULL,started_ms INTEGER NOT NULL,status INTEGER);`);
    db.prepare('INSERT INTO batch VALUES(?,?,?,?,0,0,?,NULL)').run(batchId,providerLimit,dshLimit,deadlineMs,'ACTIVE');
  }finally{db.close();}
}
function status(file){const db=connect(file);try{return db.prepare('SELECT * FROM batch').get();}finally{db.close();}}
function reserve(file,kind,requestId=randomUUID(),now=Date.now()){
  if(!['provider','dsh'].includes(kind))throw error('INVALID_BUDGET_KIND');
  const db=connect(file);let rejection=null;
  try{
    db.exec('BEGIN IMMEDIATE');
    const row=db.prepare('SELECT * FROM batch').get();
    if(!row)throw error('INVALID_BATCH');
    if(row.state!=='ACTIVE')rejection='BATCH_STOPPED';
    else if(now>=row.deadline_ms)rejection='BATCH_DEADLINE';
    else if(row.provider_used>=row.provider_limit || (kind==='dsh' && row.dsh_used>=row.dsh_limit))rejection='BATCH_LIMIT';
    if(rejection){
      if(row.state==='ACTIVE')db.prepare("UPDATE batch SET state='EXHAUSTED',stop_reason=?").run(rejection);
    }else{
      db.prepare('INSERT INTO attempts VALUES(?,?,?,NULL)').run(requestId,kind,now);
      const column=kind==='provider'?'provider_used':'dsh_used';
      db.exec(`UPDATE batch SET ${column}=${column}+1`);
    }
    db.exec('COMMIT');
  }catch(e){try{db.exec('ROLLBACK');}catch{}throw e;}finally{db.close();}
  if(rejection)throw error(rejection);
  return requestId;
}
function finish(file,requestId,httpStatus,quotaCode=null){
  const db=connect(file);
  try{
    db.exec('BEGIN IMMEDIATE');
    db.prepare('UPDATE attempts SET status=? WHERE id=?').run(httpStatus,requestId);
    if(quotaCode)db.prepare("UPDATE batch SET state='HALTED',stop_reason=?").run(quotaCode);
    db.exec('COMMIT');
  }catch(e){try{db.exec('ROLLBACK');}catch{}throw e;}finally{db.close();}
}
function quotaCode(statusCode,body){
  if(statusCode<400)return null;
  try{
    const parsed=JSON.parse(body),code=parsed?.error?.code||parsed?.code;
    return ['AccountQuotaExceeded','insufficient_quota','QuotaExceeded'].includes(code)?code:null;
  }catch{return null;}
}
async function forwardProvider({budgetPath,requestId=randomUUID(),url,init,transport=fetch}){
  if(budgetPath)reserve(budgetPath,'provider',requestId); // Atomic before any network send.
  let response;
  try{
    response=await transport(url,init);
    const chunks=[];let size=0;
    for await(const chunk of response.body){size+=chunk.length;if(size>8000000)throw error('BROKER_OUTPUT_LIMIT');chunks.push(chunk);}
    const body=Buffer.concat(chunks).toString('utf8');
    if(budgetPath)finish(budgetPath,requestId,response.status,quotaCode(response.status,body));
    return {status:response.status,body,contentType:response.headers.get('content-type')||'application/json'};
  }catch(e){if(budgetPath)finish(budgetPath,requestId,response?.status||0);throw e;}
}
module.exports={createBudget,reserve,finish,status,quotaCode,forwardProvider};
if(require.main===module){
  const [action,file,provider,dsh,minutes]=process.argv.slice(2);
  if(action==='create')createBudget(file,{providerLimit:Number(provider),dshLimit:Number(dsh),deadlineMs:Date.now()+Number(minutes)*60000});
  else if(action!=='status')throw error('USAGE_CREATE_FILE_PROVIDER_DSH_MINUTES_OR_STATUS_FILE');
  process.stdout.write(JSON.stringify(status(file))+'\n');
}
