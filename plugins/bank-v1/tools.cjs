'use strict';
const fs = require('node:fs');

exports.name = 'bank-v1-tools';
exports.inject = ['tools'];
exports.apply = function apply(ctx, config) {
  const specs = JSON.parse(fs.readFileSync(process.env.BANK_V1_REQUEST, 'utf8')).tools;
  const allowed = new Set(specs.map(spec => spec.name));
  let calls = 0;
  const record = value => {
    let serialized=JSON.stringify({at:new Date().toISOString(), ...value});
    for(const secret of [process.env.BANK_V1_TOOL_TOKEN,process.env.ARK_CODING_PLAN_API_KEY,process.env.ARK_AGENT_PLAN_API_KEY]) if(secret)serialized=serialized.split(secret).join('[REDACTED]');
    fs.appendFileSync(process.env.BANK_V1_TRACE, serialized+'\n');
  };
  ctx.tools.guard(exec => {
    const denied = !allowed.has(exec.name) || ++calls > 16;
    record({kind:'guard', name:exec.name, denied});
    return denied ? '银行模式仅允许已注册业务工具，每轮最多16次；禁止任何终端、文件、代码或自行确认能力。' : undefined;
  });
  for (const spec of specs) {
    ctx.tools.register({
      ...spec,
      timeoutMs:15000,
      output:{schema:{type:'object'},render:(_args,value)=>[{type:'text',text:JSON.stringify(value)}]},
      async execute(args, exec) {
        const names = ctx.tools.schemas(exec.agent).map(item=>item.name).sort();
        if (JSON.stringify(names)!==JSON.stringify([...allowed].sort())) throw new Error('Unexpected tool surface');
        record({kind:'request',name:spec.name,call_id:exec.callId,arguments:args});
        const response = await fetch(process.env.BANK_V1_API_BASE+'/api/tools/'+spec.name, {
          method:'POST',redirect:'error',
          headers:{'Content-Type':'application/json','Authorization':'Bearer '+process.env.BANK_V1_TOOL_TOKEN},
          body:JSON.stringify(args),signal:AbortSignal.any([exec.signal,AbortSignal.timeout(12000)])
        });
        const body = await readBoundedJson(response);
        record({kind:'response',name:spec.name,tool:spec.name,args,ok:response.ok&&!body.error&&!body.code,call_id:exec.callId,status:response.status,response:body});
        if (!response.ok && !body.code && !body.error) throw new Error('Bank HTTP '+response.status);
        return body;
      }
    });
  }
  record({kind:'registered',allowed:[...allowed].sort()});
};

async function readBoundedJson(response) {
  const chunks=[]; let length=0;
  for await(const chunk of response.body) {
    length+=chunk.length;
    if(length>1000000) throw new Error('Bank response exceeds limit');
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}
