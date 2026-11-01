import {createCompetitionWorkspace} from './competition.js';
import {createPlanWorkspace} from './plans.js';
import {createGoalWorkspace} from './goals.js';
import {renderRuntimeStatus} from './runtime.js';
import {conversationItems, failureText} from './conversation.js';
const ui={state:null,csrf:'',scope:'bank',loading:false,selectedOperation:null,limit:12,version:'',started:false};
const $=id=>document.getElementById(id);
const money=minor=>new Intl.NumberFormat('zh-CN',{style:'currency',currency:'CNY'}).format((minor||0)/100);
const names={transfer:'账户转账',arrangement:'更新安排',mandate_query:'查询自动扣款',mandate_cancel:'取消扣款授权',arrangement_match:'关联流水与安排',PENDING:'待确认',SUCCEEDED:'已完成',REJECTED:'已拒绝',CANCELLED:'已取消',EXPIRED:'已过期',active:'生效中',cancelled:'已取消',income:'预计收入',expense:'支出',goal:'目标'};
const label=v=>names[v]||v;
function stableCards(container, items, getKey, render) {
  const existing = new Map([...container.children].map(node => [node.dataset.renderKey, node]));
  const nodes = items.map(item => {
    const key = getKey(item), version = JSON.stringify(item), previous = existing.get(key);
    if (previous?.sourceVersion === version) return previous;
    const node = render(item);
    node.dataset.renderKey = key;
    node.sourceVersion = version;
    if (previous) {
      if (node.tagName === 'DETAILS') node.open = previous.open;
      const oldDetails = [...previous.querySelectorAll('details')];
      node.querySelectorAll('details').forEach((detail, index) => { detail.open = oldDetails[index]?.open || false; });
      const oldAnswer = previous.querySelector('.chat-answer');
      if (oldAnswer && node.querySelector('.chat-answer')) node.querySelector('.chat-answer').scrollTop = oldAnswer.scrollTop;
    }
    return node;
  });
  if (nodes.length !== container.children.length || nodes.some((node, index) => node !== container.children[index])) container.replaceChildren(...nodes);
}

function element(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}
function showError(error) {
  $('error').textContent = error?.message || String(error);
  $('error').hidden = false;
  const dialog = [...document.querySelectorAll('dialog[open]')].at(-1);
  if (dialog) { dialog.querySelector('.dialog-error')?.remove(); const message = element('p', 'dialog-error warning', error?.message || String(error)); message.setAttribute('role', 'alert'); dialog.append(message); }
}
async function api(path, body) {
  const options = body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json', 'X-CSRF-Token': ui.csrf}, body: JSON.stringify(body)};
  const response = await fetch(path, options);
  const data = await response.json();
  if (!response.ok) throw new Error(data.error?.message || '请求未完成，请查看原状态后重试。');
  return data;
}
async function busy(button, operation) {
  if (button.disabled) return;
  button.disabled = true;
  button.dataset.busy = 'true';
  $('error').hidden = true;
  document.querySelectorAll('.dialog-error').forEach(node => node.remove());
  try { await operation(); } catch (error) { showError(error); } finally {
    delete button.dataset.busy; button.disabled = false;
    if (ui.state) renderRuntimeStatus(ui.state, document);
  }
}
function details(value, title = '查看明细') {
  const node = element('details');
  node.append(element('summary', '', title), element('pre', '', JSON.stringify(value, null, 2)));
  return node;
}
function inlineText(node, text) {
  for (const part of text.split(/(\*\*[^*]+\*\*|`[^`]+`)/g)) {
    node.append(part.startsWith('**') && part.endsWith('**') ? element('strong', '', part.slice(2, -2)) : document.createTextNode(part.startsWith('`') && part.endsWith('`') ? part.slice(1, -1) : part));
  }
  return node;
}
function answerBody(text) {
  // A small text-only formatter: model HTML is never interpreted or executed.
  const node = element('div', 'chat-answer');
  const lines = String(text).split('\n');
  for (let index = 0; index < lines.length; index++) {
    const line = lines[index].trim();
    if (!line) continue;
    if (line.startsWith('|') && lines[index + 1]?.match(/^\s*\|[\s:|\-]+\|\s*$/)) {
      const wrapper = element('div', 'chat-table'), table = element('table');
      let first = true;
      while (index < lines.length && lines[index].trim().startsWith('|')) {
        const current = lines[index].trim();
        if (!/^\|[\s:|\-]+\|$/.test(current)) {
          const row = element('tr');
          current.replace(/^\||\|$/g, '').split('|').forEach(cell => row.append(inlineText(element(first ? 'th' : 'td'), cell.trim())));
          table.append(row); first = false;
        }
        index++;
      }
      index--; wrapper.append(table); node.append(wrapper); continue;
    }
    if (/^#{1,6}\s/.test(line)) node.append(inlineText(element('h3'), line.replace(/^#{1,6}\s+/, '')));
    else node.append(inlineText(element('p'), line));
  }
  return node;
}
function operationLines(operation) {
  const p = operation.summary.params;
  const lines = [];
  if (p.source_account) lines.push(`账户 ${p.source_account} → ${p.target_account}`);
  if (p.title) lines.push(p.title);
  if (p.amount_minor !== undefined) lines.push(`金额：${money(p.amount_minor)}`);
  if (p.reserve_minor !== undefined) lines.push(`转账后转出账户至少保留：${money(p.reserve_minor)}（确认时重新检查）`);
  if (p.due_date) lines.push(`日期：${p.due_date}`);
  if (p.kind) lines.push(`类型：${label(p.kind)}`);
  if (p.status) lines.push(`安排状态：${label(p.status)}`);
  if (p.mandate_id) lines.push(`授权编号：${p.mandate_id}`);
  const mandate = operation.summary.mandate_snapshot;
  if (operation.kind === 'mandate_cancel' && mandate) {
    lines.push(`商户：${mandate.name} · 授权版本 ${mandate.version}`);
    if (p.query_operation_id) lines.push(`来源查询：${p.query_operation_id}`);
  }
  if (p.arrangement_id) lines.push(`安排编号：${p.arrangement_id}，依据版本 ${p.expected_version ?? '新建'}`);
  if (p.transaction_id) lines.push(`关联流水：${p.transaction_id}，入账日期 ${p.transaction_date}。只完成对应安排，不再次记账。`);
  return lines;
}
function openConfirm(operation) {
  ui.selectedOperation = structuredClone(operation);
  $('confirm-details').replaceChildren(element('p', '', label(operation.kind)), ...operationLines(operation).map(line => element('p', '', line)), element('p', 'caption', `有效期至 ${new Date(operation.expires_at).toLocaleString('zh-CN')}`));
  const independent = ui.state?.security?.mode === 'independent';
  $('challenge-field').hidden = operation.required_verification !== 'STRONG' || independent;
  $('independent-field').hidden = operation.required_verification !== 'STRONG' || !independent;
  $('verify-password').value = ''; $('verify-otp').value = '';
  $('challenge-answer').value = '';
  $('challenge-text').textContent = '';
  $('confirm-dialog').showModal();
}
function operationReceipt(item) {
  const card = element('article', 'operation');
  const expired = item.status === 'PENDING' && item.is_expired;
  card.append(element('h3', '', `${label(item.kind)} · ${expired ? '确认已过期' : label(item.status)}`));
  const receipt = item.receipt;
  if (receipt?.kind === 'mandate_query') {
    card.append(element('p', 'caption', '以下是这次查询时的银行登记，不代表商户会员合同。'));
    for (const mandate of receipt.mandates || []) card.append(element('p', '', `${mandate.name}（${mandate.mandate_id}）· ${money(mandate.amount_minor)} · ${label(mandate.status)}`));
    if (!(receipt.mandates || []).length) card.append(element('p', '', '本次查询没有返回银行代扣授权。'));
  } else if (receipt?.kind === 'mandate_cancel') {
    card.append(element('p', '', `授权 ${receipt.mandate_id} · ${label(receipt.mandate_status || 'CANCELLED')}`), element('p', 'caption', '仅取消银行扣款授权，商户服务和应付费用仍需另行处理。'));
  } else {
    card.append(element('p', '', operationLines(item).join('\n')));
  }
  if (expired) card.append(element('p', 'caption', '未执行。原明细保留供核对；重新办理需要新的有效确认。'));
  if (receipt?.committed_at) card.append(element('p', 'caption', `办理时间 ${new Date(receipt.committed_at).toLocaleString('zh-CN')}`));
  card.append(details(receipt || item, '查看原始回执与依据'));
  return card;
}
$('close-confirm').addEventListener('click', () => $('confirm-dialog').close());
$('get-challenge').addEventListener('click', () => busy($('get-challenge'), async () => {
  const challenge = await api('/api/actions/challenge', {operation_id: ui.selectedOperation.operation_id});
  $('challenge-text').textContent = `模拟验证码：${challenge.challenge_answer}，请在下方输入。`;
}));
$('confirm-submit').addEventListener('click', () => busy($('confirm-submit'), async () => {
  const operation = ui.selectedOperation;
  const body = {operation_id: operation.operation_id, expected_summary: operation.summary};
  if (operation.required_verification === 'STRONG') {
    if (ui.state?.security?.mode === 'independent') {
      try {
        const grant = await api('/api/competition/security/verify', {...body, password:$('verify-password').value, otp:$('verify-otp').value.trim()});
        body.verification_token = grant.verification_token;
      } finally { $('verify-password').value = ''; $('verify-otp').value = ''; }
    } else body.challenge_answer = $('challenge-answer').value.trim();
  }
  await api('/api/actions/confirm', body); $('confirm-dialog').close(); await refresh(); await plans.confirmed(operation);
}));
const plans=createPlanWorkspace({api,refresh,openConfirm,element,money});
const competition=createCompetitionWorkspace({api,busy,refresh,element,details,money,stableCards});
const goals=createGoalWorkspace({api,busy,refresh,element,details,money,stableCards});
function action(text,handler,style='secondary') {const b=element('button',style,text);b.type='button';b.addEventListener('click',()=>busy(b,handler));return b;}
function openGoals(){if($('settings-dialog').open)$('settings-dialog').close();$('goal-dialog').showModal();}
function operationCard(op) {
  const expired=op.status==='PENDING'&&Date.parse(op.expires_at)<=Date.now();
  if(op.status!=='PENDING'||expired){const n=operationReceipt({...op,is_expired:expired});n.classList.add('chat-card');return n;}
  const n=element('article','chat-card');n.append(element('h3','','需要你确认 · '+label(op.kind)),...operationLines(op).map(t=>element('p','',t)),element('p','caption','核对明细后才会执行；聊天消息不会代替你的确认。'));
  const buttons=element('div','operation-buttons');buttons.append(action('核对并确认',async()=>openConfirm(op),'primary'),action('拒绝',async()=>{await api('/api/actions/reject',{operation_id:op.operation_id});await refresh();}));n.append(buttons);return n;
}
function jobCard(job) {
  const proactive=job.kind!=='chat', n=element('article',proactive?'chat-proactive':'chat-pair');n.dataset.jobId=job.id;
  n.append(element(proactive?'h3':'div',proactive?'':'chat-user',proactive?(job.kind==='goal'?'收支有度 · 目标跟进':'收支有度 · 主动检查'):(job.retry_of?'重新核对上次问题（只读恢复）':job.prompt)));
  if(job.status==='DONE'&&job.result?.ok){n.append(answerBody(job.result.text));const trace=job.result.trace?.filter(t=>t.kind==='response')||[];if(trace.length){const d=details(trace,`查看 ${trace.length} 次工具调用依据`);d.className='trace-detail';n.append(d);}}
  else if(['FAILED','INTERRUPTED'].includes(job.status)){
    n.append(element('p','chat-failed',failureText(job.result)));
    if(job.kind==='chat'){const retry=action('重新查询（不重复办理）',async()=>{await api(`/api/jobs/${job.id}/retry`,{});await refresh();});retry.disabled=!ui.state.model?.ready_for_requests||ui.state.jobs.some(j=>j.retry_of===job.id);n.append(retry);}
    else if(job.kind==='event'){const eventId=job.event_key?.split(':')[1];const event=ui.state.events?.find(e=>e.event_id===eventId);if(event?.state==='FAILED'&&event.attempts>=3)n.append(action('重新分析',async()=>{await api('/api/actions/retry_event',{event_id:eventId});await refresh();}));}
    else if(ui.state.competition?.goals?.followup?.service_health?.can_retry)n.append(action('重新检查目标',async()=>{const health=ui.state.competition.goals.followup.service_health;await api('/api/competition/goal-followup/retry',{expected_check_id:health.last_attempt_check_id});await refresh();}));
  }else n.append(element('p','chat-pending',job.status==='RUNNING'?'正在查资料、调用工具分析…':'已收到，正在等待分析…'));
  for(const entry of job.result?.host_disclosure?.entries||[]) n.append(element('p','chat-meta',entry.text));
  if(job.result?.host_disclosure?.complete===false) n.append(element('p','chat-meta','这次处理的历史记录尚无法完整核对。已有结果以原记录的当前状态为准，请勿重复办理。'));
  if(job.result?.recovery?.kind==='REVIEW_GOAL_FORM'&&ui.state.model?.capabilities?.goals){
    n.append(action('打开目标表单核对',async()=>openGoals()));
  }
  n.append(element('div','chat-meta',new Date(job.created_at).toLocaleString('zh-CN')+(proactive?' · 本次检查时的记录':'')));return n;
}
function renderConversation(state){
  const all=conversationItems(state,ui.scope);
  const version=JSON.stringify([ui.scope,ui.limit,all,state.competition?.tasks,state.competition?.insights?.data?.candidates,state.competition?.goals?.drafts,state.security?.locked,state.match_candidates,state.model?.ready_for_requests]);
  if(version===ui.version)return;ui.version=version;
  const box=$('conversation'), nearBottom=!ui.started||box.scrollHeight-box.scrollTop-box.clientHeight<140;
  const oldHeight=box.scrollHeight,oldTop=box.scrollTop,expanded=new Set([...box.querySelectorAll('details[open]')].map(n=>n.dataset.key).filter(Boolean));
  let visible=all.slice(-ui.limit);
  for(const item of all)if(item.type==='operation'&&item.operation.status==='PENDING'&&Date.parse(item.operation.expires_at)>Date.now()&&!visible.includes(item))visible.push(item);
  visible.sort((a,b)=>Date.parse(a.at)-Date.parse(b.at));
  box.replaceChildren();
  if(all.length>ui.limit)box.append(action(`查看之前的 ${all.length-ui.limit} 条记录`,async()=>{ui.historyExpanding=true;ui.limit+=20;renderConversation(ui.state);ui.historyExpanding=false;},'chat-history'));
  if(!all.length){const welcome=element('div','chat-welcome');welcome.append(element('h2','','今天想先聊什么？'),element('p','','查余额、看看支出的影响，或告诉我一个需要跟进的计划。'));box.append(welcome);}
  for(const item of visible){const node=item.type==='job'?jobCard(item.job):operationCard(item.operation);node.dataset.key=item.key;node.querySelectorAll('details').forEach((d,i)=>{d.dataset.key=item.key+':'+i;d.open=expanded.has(d.dataset.key);});box.append(node);}
  if(ui.scope==='bank'){
    competition.render(state,'bank');
    const active=(state.competition?.tasks||[]).filter(t=>!['SUCCEEDED','CANCELLED'].includes(t.state));
    const taskNodes=[...$('task-list').children].filter(n=>active.some(t=>n.dataset.renderKey===t.task_id));
    const proposed=new Set((state.competition?.insights?.data?.candidates||[]).filter(c=>c.status==='proposed').map(c=>c.candidate_id));
    const candidates=[...$('insight-candidates').children].filter(n=>proposed.has(n.dataset.renderKey));
    if(taskNodes.length||candidates.length){const d=element('details','chat-card chat-inline-actions');d.dataset.key='followup-actions';d.open=expanded.has(d.dataset.key);d.append(element('summary','',`需要跟进：${taskNodes.length} 项任务 · ${candidates.length} 项流水规律待核对`),...taskNodes,...candidates);box.append(d);}
    for(const item of state.match_candidates||[]){
      const card=element('article','chat-card');card.append(element('h3','','这笔到账是否对应已有安排？'),element('p','',`${item.title} · ${item.transaction_date} 已入账 ${money(item.amount_minor)}，可能对应 ${item.due_date} 的安排。`),element('p','caption','需要你核对，关联后只完成安排，不再记账。'));
      const buttons=element('div','operation-buttons');buttons.append(action('核对关联',async()=>{const op=await api('/api/actions/prepare_match',{transaction_id:item.transaction_id,arrangement_id:item.arrangement_id,expected_version:item.expected_version,idempotency_key:crypto.randomUUID()});await refresh();openConfirm(op);}),action('不对应此安排',async()=>{await api('/api/actions/reject_match',{transaction_id:item.transaction_id,arrangement_id:item.arrangement_id,expected_version:item.expected_version});await refresh();}));card.append(buttons);box.append(card);
    }
    if(state.competition?.goals?.drafts?.length){const n=element('article','chat-card');n.append(element('h3','','收支有度准备了目标草案'),element('p','','先核对目标和金额，再由你保存。'),action('核对目标草案',async()=>openGoals()));box.append(n);}
    if(state.security?.locked)box.append(action('操作已锁定，进行独立验证',async()=>$('unlock-dialog').showModal()));
  }
  if(nearBottom){box.scrollTop=box.scrollHeight;$('new-message').hidden=true;}
  else {box.scrollTop=oldTop; $('new-message').hidden=false;if(ui.historyExpanding)box.scrollTop=oldTop+box.scrollHeight-oldHeight;}
  ui.started=true;
}
async function refresh(){
  if(ui.loading)return;ui.loading=true;
  try{const state=await api('/api/state');ui.state=state;ui.csrf=state.csrf_token;
    if(document.activeElement!==$('cadence'))$('cadence').value=state.schedule.enabled?state.schedule.cadence:'off';
    plans.render(state);goals.render(state,ui.scope);renderRuntimeStatus(state,document);renderConversation(state);
    const unavailable=state.model?.managed&&!state.model.ready_for_requests;
    $('service-notice').hidden=!unavailable;$('service-notice').textContent=state.model?.reason||'';
    $('check-now').disabled=unavailable||ui.scope!=='bank'||$('check-now').dataset.busy==='true';
    if($('error').dataset.connection){$('error').hidden=true;delete $('error').dataset.connection;}
  }finally{ui.loading=false;}
}
async function sendPrompt(prompt){if(ui.state?.model?.managed&&!ui.state.model.ready_for_requests)throw new Error(ui.state.model.reason);await api('/api/chat',{prompt,scope:ui.scope});$('prompt').value='';$('conversation').scrollTop=$('conversation').scrollHeight;await refresh();}
$('chat-form').addEventListener('submit',e=>{e.preventDefault();const value=$('prompt').value.trim();if(value)busy($('send'),()=>sendPrompt(value));});
$('prompt').addEventListener('keydown',e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();$('chat-form').requestSubmit();}});
document.querySelectorAll('[data-prompt]').forEach(b=>b.addEventListener('click',()=>busy(b,()=>sendPrompt(b.dataset.prompt))));
$('settings-open').addEventListener('click',()=>$('settings-dialog').showModal());
document.querySelectorAll('[data-close]').forEach(b=>b.addEventListener('click',()=>$(b.dataset.close).close()));
$('goals-open').addEventListener('click',openGoals);
$('cadence').addEventListener('change',()=>busy($('cadence'),async()=>{await api('/api/schedule',{enabled:$('cadence').value!=='off',cadence:$('cadence').value==='off'?'daily':$('cadence').value});await refresh();}));
$('scope').addEventListener('change',()=>{ui.scope=$('scope').value;ui.started=false;$('settings-dialog').close();$('prompt').placeholder=ui.scope==='bank'?'说说你的需求，或接着问…':'分析 MoneyData 历史资料（非当前银行账户）';refresh().catch(showError);});
$('check-now').addEventListener('click',()=>busy($('check-now'),async()=>{ui.checkAttempt ||= crypto.randomUUID();await api('/api/actions/due_check',{idempotency_key:ui.checkAttempt});ui.checkAttempt=null;$('conversation').scrollTop=$('conversation').scrollHeight;await refresh();}));
$('new-message').addEventListener('click',()=>{$('conversation').scrollTop=$('conversation').scrollHeight;$('new-message').hidden=true;});
function connectionError(error){showError(new Error('暂时连不上本机服务。请双击“启动有数”；恢复后本页会自动重连，记录仍保留。'));$('error').dataset.connection='true';$('agent-status').textContent='服务未连接';$('send').disabled=true;}
refresh().catch(connectionError);setInterval(()=>refresh().catch(connectionError),3500);
