'use strict';
const fs = require('node:fs');
const path = require('node:path');
const {createRequire} = require('node:module');
const {pathToFileURL} = require('node:url');
function writeResult(value) {
  let text=JSON.stringify(value);
  for(const secret of [process.env.BANK_V1_TOOL_TOKEN,process.env.ARK_CODING_PLAN_API_KEY,process.env.ARK_AGENT_PLAN_API_KEY]) if(secret)text=text.split(secret).join('[REDACTED]');
  fs.writeFileSync(process.env.BANK_V1_RESULT,text);
}
exports.name='bank-v1-driver';
exports.inject=['agents','sessions','agentDefaultModel','agentPresets','tools'];
exports.apply=function apply(ctx) {
  run(ctx).catch(error=>{
    writeResult({ok:false,error:{code:'DSH_DRIVER_FAILED',message:String(error.message).slice(0,2000)}});
    ctx.get('appExit')(1);
  });
};
async function run(ctx) {
  await ctx.get('loader')?.await();
  const req=createRequire(path.join(process.env.BANK_V1_DSH_PACKAGE,'package.json'));
  const {installModelSelection}=await import(pathToFileURL(req.resolve('@deepseek-ai/dsh-agent')).href);
  const {createUserMessage}=await import(pathToFileURL(req.resolve('@deepseek-ai/dsh-llm')).href);
  const {scopeOf}=await import(pathToFileURL(req.resolve('@deepseek-ai/dsh-scope')).href);
  const input=JSON.parse(fs.readFileSync(process.env.BANK_V1_REQUEST,'utf8'));
  const selection=ctx.agentDefaultModel.currentSelection();
  const setup=async agentCtx=>{
    await ctx.agentPresets.mount(agentCtx,'bank-v1');
    installModelSelection(agentCtx,{current:selection,assembled:undefined});
    const tools=agentCtx.tools.schemas(scopeOf(agentCtx)).map(t=>t.name).sort();
    fs.appendFileSync(process.env.BANK_V1_TRACE,JSON.stringify({kind:'surface',tools,preset:'bank-v1',provider:selection.provider,model:selection.model})+'\n');
    if(JSON.stringify(tools)!==JSON.stringify(input.tools.map(t=>t.name).sort())) throw new Error('Non-bank tool surface detected');
  };
  const options={agentOptions:{provider:selection.provider,model:selection.model,
    ...(input.data_scope==='moneydata'?{maxTokens:4096}:{})},setup};
  const handle=input.resume
    ? await ctx.agents.resume({...options,resumeSessionId:input.session_id})
    : await ctx.agents.create({...options,sessionId:input.session_id,meta:{cwd:process.cwd(),agentPreset:'bank-v1'}});
  const agent=handle.agent;
  await agent.whenIdle();
  const firstSeq=agent.session.seq;
  const localToday=new Date(Date.now()+8*60*60*1000).toISOString().slice(0,10);
  const dateContext=input.data_scope==='moneydata'
    ? '可信本机今日（Asia/Shanghai）为 '+localToday+'。当前为 MoneyData 独立历史工作区。先读取 overview 确认历史截止日、币种和数据边界。历史截止日不是今日，历史余额不是当前可花的钱，不能与人民币模拟账户混用。\n'
    : input.business_date
    ? '可信银行业务日期（Asia/Shanghai）为 '+input.business_date+'。这是本轮今日日期；observed_at可能是UTC时间，不可直接截断为北京时间日期。\n'
    : '本轮按Asia/Shanghai解释日期；工具observed_at可能是UTC时间，不可直接截断为北京时间日期。forecast.as_of_date是银行业务今日。\n';
  agent.followup(createUserMessage({content:[{type:'text',text:dateContext+input.prompt}],source:{kind:'user'}}));
  await agent.whenIdle();
  await ctx.sessions.flush(agent.session);
  let text='',reason;
  for(const event of agent.session.events) {
    if(event.seq<firstSeq) continue;
    if(event.type==='assistant/message') {
      const value=event.data.message.content.filter(b=>b.type==='text').map(b=>b.text).join('');
      if(value) text=value;
    }
    if(event.type==='turn/end') reason=event.data.reason;
  }
  const ok=reason?.kind==='completed';
  writeResult({ok,text,session_id:input.session_id,preset:'bank-v1',provider:selection.provider,model:selection.model,...ok?{}:{error:{code:'DSH_TURN_FAILED',reason}}});
  ctx.get('appExit')(ok?0:1);
}
