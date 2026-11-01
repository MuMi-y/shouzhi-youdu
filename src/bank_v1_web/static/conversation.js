/* Stable timeline identities reuse durable jobs; rendering never starts AI or banking work. */
export function conversationItems(state, scope='bank') {
  const items=[], events=new Map();
  for (const job of state.jobs || []) {
    if ((job.scope || 'bank') !== scope) continue;
    if (!['chat','event','goal'].includes(job.kind)) continue;
    if (job.kind === 'event' && state.competition?.goals?.followup?.event_handling === 'GOAL_FOLLOWUP' && !job.result?.ok && !job.result?.host_disclosure?.entries?.length && job.result?.host_disclosure?.complete!==false) continue;
    const key=job.kind === 'event' ? 'event:' + job.event_key?.split(':')[1] : 'job:' + job.id;
    const item={key,type:'job',at:job.created_at,job};
    if(job.kind === 'event') events.set(key,item); else items.push(item);
  }
  // Events persist before the worker has a chance to create a model job.
  if(scope==='bank'&&state.competition?.goals?.followup?.event_handling!=='GOAL_FOLLOWUP') {
    for(const event of state.events||[]) if(event.state==='PENDING'&&!events.has('event:'+event.event_id)) {
      const key='event:'+event.event_id;
      events.set(key,{key,type:'job',at:event.created_at,job:{id:key,kind:'event',event_key:'event:'+event.event_id+':pending',created_at:event.created_at,status:'QUEUED'}});
    }
  }
  items.push(...events.values());
  if(scope==='bank') for(const op of state.operations || []) items.push({key:'operation:'+op.operation_id,type:'operation',at:op.created_at,expired:Date.parse(op.expires_at)<=Date.now(),operation:op});
  return items.sort((a,b)=>Date.parse(a.at)-Date.parse(b.at)||a.key.localeCompare(b.key));
}
export function failureText(result) {
  const code=result?.error?.code;
  if(code==='DSH_VERSION'||code==='DSH_MISSING') return '本地 AI 运行依赖缺失或版本不匹配，这次没有调用模型。修复依赖并重新启动后，可重新查询。';
  if(code==='TIMEOUT'||code==='PROCESS_TIMEOUT') return '模型未在本轮时限内完成回答。这次分析没有完成；已有记录仍保留，可重新查询核对。';
  const providerError=result?.error?.reason?.error;
  if(providerError?.code==='SERVER'&&/^500[: ]/.test(providerError.message||'')) return '模型服务返回了内部错误（HTTP 500），本次没有完成。银行记录仍保留；稍后可以重新查询核对。';
  return result?.error_message || (typeof result?.error==='string'?result.error:result?.error?.message) || '这次分析未完成。已有银行记录保留，可重新查询核对，不会自动重复办理。';
}
