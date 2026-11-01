'use strict';
const {test}=require('node:test'),assert=require('node:assert/strict');
const fs=require('node:fs'),path=require('node:path'),vm=require('node:vm');
function fixture(file) {
  const writes=[],secret='FAKE_AGENT_PLAN_SECRET',exports={};
  const fakeFs={readFileSync:()=>'{"tools":[]}',
    appendFileSync:(_file,text)=>writes.push(text),writeFileSync:(_file,text)=>writes.push(text)};
  const context={exports,process:{env:{ARK_AGENT_PLAN_API_KEY:secret}},
    require:name=>name==='node:fs'?fakeFs:require(name)};
  vm.runInNewContext(fs.readFileSync(path.join(__dirname,'../../plugins/bank-v1',file),'utf8'),context);
  return {exports,writes,secret};
}
test('tool guard records redact the Agent Plan credential',()=>{
  const f=fixture('tools.cjs');let guard;
  f.exports.apply({tools:{guard:callback=>{guard=callback;}}},{});
  guard({name:f.secret});
  const guards=f.writes.map(text=>JSON.parse(text)).filter(row=>row.kind==='guard');
  assert.equal(guards.length,1);
  assert(f.writes.every(text=>!text.includes(f.secret)));
  assert.equal(guards[0].name,'[REDACTED]');
});
test('driver error output redacts the Agent Plan credential',async()=>{
  const f=fixture('driver.cjs');let exit;
  f.exports.apply({get:name=>name==='loader'?{await:async()=>{throw Error(f.secret);}}:code=>{exit=code;}});
  await new Promise(resolve=>setImmediate(resolve));
  assert.equal(exit,1);assert.equal(f.writes.length,1);
  assert(!f.writes[0].includes(f.secret));
  assert(f.writes[0].includes('[REDACTED]'));
});
