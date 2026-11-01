'use strict';

export function createCompetitionWorkspace({api, busy, refresh, element, details, money, stableCards}) {
  const $ = id => document.getElementById(id);
  let enabled = false;
  const stateNames = {READY:'继续执行中',PENDING:'等待前序',SUCCEEDED:'已完成',FAILED:'需要处理',WAITING_CONFIRMATION:'等你确认',WAITING_VERIFICATION:'等你独立验证',PAUSED:'已暂停',CANCELLED:'已取消',NEEDS_HUMAN:'已交给本人',REJECTED:'已拒绝',EXPIRED:'已过期'};
  function button(text, action, className='secondary') {
    const node = element('button', className, text);
    node.type = 'button'; node.addEventListener('click', () => busy(node, action));
    return node;
  }
  const businessNames={money_calculate:'计算金额与比例',accounts:'查询账户余额',transactions:'核对银行流水',spending_summary:'汇总收支',billing_report:'分析期间账单',arrangements:'读取已有安排',forecast:'测算未来现金流',prepare_transfer:'准备账户调拨',prepare_mandate_query:'准备查询代扣授权',prepare_mandate_cancel:'准备取消代扣授权',match_mandate_from_query:'匹配已核实的代扣',propose_arrangement:'准备更改安排',operation:'核对银行回执',recurring_candidates:'查看周期支出候选',insight_status:'查看持续关注记录',check_insights:'检查流水规律'};
  function taskCard(task) {
    const card = element('article', 'task-card');
    const heading = element('div','section-line');
    const revised = task.plan_version > 1;
    heading.append(element('h3','',revised ? '已调整的任务' : task.title),element('span','tag',stateNames[task.state] || task.state));
    card.append(heading, element('p','caption',(revised ? '原始需求：' : '') + task.prompt));
    if(revised) card.append(element('p','caption',`当前为第 ${task.plan_version} 版。现行参数见以下步骤，办理前仍须核对银行确认卡。`));
    const list=element('ol','task-nodes');
    for(const node of task.nodes) {
      const row=element('li','task-node');
      row.append(element('strong','',`${businessNames[node.tool] || '业务步骤'} · ${stateNames[node.state] || node.state}`),element('p','caption',node.depends_on.length ? '前置步骤：'+node.depends_on.map(id=>businessNames[task.nodes.find(n=>n.id===id)?.tool] || id).join('、') : '可独立开始'));
      if(node.tool === 'prepare_transfer' && Number.isSafeInteger(node.args?.amount_minor)) {
        row.append(element('p','',`当前计划金额：${money(node.args.amount_minor)}`));
      }
      if(node.error) row.append(element('p','warning',typeof node.error==='string'?node.error:(node.error.message || JSON.stringify(node.error))));
      if(node.operation_id && node.state.startsWith('WAITING')) row.append(element('p','', node.bank_status==='SUCCEEDED' ? '银行已完成；任务暂停时不推进后续步骤，继续后会核对回执。' : '请到“需要你确认”核对这项操作，确认后会继续。'));
      if(node.result) row.append(details(node.result,'查看这一步的实际依据'));
      if(node.state==='FAILED') row.append(button('重试这一步',async()=>{await api(`/api/competition/tasks/${task.task_id}/retry`,{node_id:node.id});await refresh();}));
      list.append(row);
    }
    card.append(list);
    const controls=element('div','operation-buttons');
    const control=(action,text,body={})=>button(text,async()=>{await api(`/api/competition/tasks/${task.task_id}/${action}`,body);await refresh();});
    if(['READY','WAITING_CONFIRMATION','WAITING_VERIFICATION'].includes(task.state)) controls.append(control('pause','暂停任务'));
    if(['PAUSED','NEEDS_HUMAN'].includes(task.state)) controls.append(control('resume','继续任务'));
    if(!['SUCCEEDED','CANCELLED'].includes(task.state)) {
      controls.append(control('handoff','交给我处理',{reason:'本人从网页接管'}),control('cancel','取消未完成任务'));
      card.append(element('p','caption','暂停或接管不会撤销银行操作；取消前请先拒绝已有待确认操作。已完成交易保留回执。'));
    }
    card.append(controls,details(task.audit,'任务过程记录'));
    return card;
  }
  function feedback(item,decision,extra={}) {return api('/api/competition/candidate-feedback',{candidate_id:item.candidate_id,expected_version:item.version,decision,...extra});}
  function candidateCard(item) {
    const node=element('article','operation');
    const states={proposed:'待核对',confirmed:'正在关注',ignored:'已忽略',rejected:'已否认'};
    node.append(element('h3','',item.label || item.merchant || '周期支出候选'),element('p','',`${money(item.amount_minor)} · 推算下次 ${item.next_due_date}`),element('p','caption',`${states[item.status] || item.status} · ${item.evidence_refs.length} 条依据${item.currently_observed === false ? ' · 近期依据已不足' : ''}`));
    node.append(element('p','caption','这是流水规律推断。确认后仅持续关注；正式代扣需另行核对授权。'));
    const actions=element('div','operation-buttons');
    for(const [decision,title] of [['confirm','确认关注'],['reject','不是周期支出'],['ignore','忽略']]) actions.append(button(title,async()=>{await feedback(item,decision);await api('/api/competition/insights-check',{});await refresh();}));
    actions.append(button('更正',async()=>{
      $('candidate-id').value=item.candidate_id; $('candidate-version').value=item.version;
      $('candidate-label').value=item.label || item.merchant || '周期支出';
      $('candidate-amount').value=(item.amount_minor/100).toFixed(2); $('candidate-date').value=item.next_due_date;
      $('candidate-dialog').showModal();
    }));
    node.append(actions,details(item,'查看原始依据与更正记录'));
    return node;
  }
  $('candidate-close').addEventListener('click',()=>$('candidate-dialog').close());
  $('candidate-form').addEventListener('submit',event=>{event.preventDefault();busy($('candidate-save'),async()=>{
    const value=$('candidate-amount').value;
    if(!/^\d+(\.\d{1,2})?$/.test(value)) throw new Error('金额最多两位小数。');
    const [whole,fraction='']=value.split('.');
    await feedback({candidate_id:$('candidate-id').value,version:Number($('candidate-version').value)},'correct',{label:$('candidate-label').value.trim(),amount_minor:Number(whole)*100+Number(fraction.padEnd(2,'0')),next_due_date:$('candidate-date').value});
    await api('/api/competition/insights-check',{});$('candidate-dialog').close();await refresh();
  });});
  $('security-unlock-open').addEventListener('click',()=>$('unlock-dialog').showModal());
  $('unlock-close').addEventListener('click',()=>$('unlock-dialog').close());
  $('unlock-form').addEventListener('submit',event=>{event.preventDefault();busy($('unlock-submit'),async()=>{
    try {await api('/api/competition/security/unlock',{password:$('unlock-password').value,otp:$('unlock-otp').value,reason:'本人独立验证解除锁定'});$('unlock-dialog').close();await refresh();}
    finally {$('unlock-password').value='';$('unlock-otp').value='';}
  });});
  function render(state,scope) {
    enabled=Boolean(state.competition);
    document.querySelectorAll('[data-competition]').forEach(node=>node.hidden=!enabled || scope!=='bank');
    if(!enabled)return;
    $('edition').textContent='收支有度 · 参赛开发版';
    const security=state.security || {};
    $('security-state').textContent=security.locked?'操作已锁定':(security.enrolled?'独立验证已绑定':'独立验证未绑定');
    $('security-description').textContent=security.locked?'连续验证或越权失败触发锁定；只读查询仍可用。':(security.provenance==='TEST_FIXTURE'?'当前使用测试身份，不能当作你本人的绑定。':security.enrolled?'涉及大额或累计超限操作时，需要独立口令与认证器。':'普通查询和确认可体验；大额操作会被阻止，须先完成本人独立绑定。');
    $('security-unlock-open').hidden=!security.locked;
    const tasks=state.competition.tasks.map(task=>({...task,nodes:task.nodes.map(node=>({...node,bank_status:state.operations.find(op=>op.operation_id===node.operation_id)?.status}))}));
    stableCards($('task-list'),tasks,task=>task.task_id,taskCard);
    if(!tasks.length)$('task-list').replaceChildren(element('p','empty','提出多步骤需求后，计划和每一步实际结果会出现在这里。'));
    const insights=state.competition.insights.data;
    stableCards($('insight-candidates'),insights.candidates,item=>item.candidate_id,candidateCard);
    if(!insights.candidates.length)$('insight-candidates').replaceChildren(element('p','empty','暂未发现足够稳定的周期规律，不会凭空推断计划。'));
    const groups=insights.notification_groups || [];
    const reasonNames={RENEWAL_DUE:'即将到推算日期',PRICE_INCREASE:'近期出账金额上升',MISSED_EXPECTED_CHARGE:'暂未观察到预期扣费'};
    stableCards($('insight-notices'),groups,group=>group.group_id,group=>{
      const card=element('article','operation');
      card.append(element('h3','',group.label),element('p','',`${money(group.amount_minor)} · 推算日期 ${group.due_date}`));
      card.append(element('p','warning',group.reasons.map(reason=>reasonNames[reason.kind] || '需要核对').join('；')));
      for(const reason of group.reasons) card.append(element('p','caption',reason.message));
      card.append(details(group,'查看各项提醒依据'));
      return card;
    });
  }
  return {render};
}
