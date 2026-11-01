/* Trusted form workflow; no model output is treated as confirmation. */
export function createGoalWorkspace({api, busy, refresh, element, details, money, stableCards}) {
  const $ = id => document.getElementById(id);
  let current = null, formRevision = null;
  let historyStale = null;
  const optionHistory=element('details','goal-history');
  const optionHistoryTitle=element('summary','','候选方案');
  $('goal-options').before(optionHistory);optionHistory.append(optionHistoryTitle,$('goal-options'));
  const followupBox=element('div','goal-followup');
  $('goal-panel').append(followupBox);
  const post = (action, body) => api(`/api/competition/goals/${action}`, body);
  const states = {SELECTED:'已选择，尚未保存银行安排',WAITING_CONFIRMATION:'安排待确认，请查看下方「需要你确认」',
    LINK_PENDING:'银行操作已成功，目标待核对原回执',LINKED:'安排已保存，不代表已经付款',EXPIRED:'确认已过期，请重新计算选择',REJECTED:'原安排卡已拒绝',STALE:'原选择依据已变化，请重新计算选择'};
  function button(label, action) {
    const b = element('button', 'secondary', label); b.type='button';
    b.addEventListener('click', () => busy(b, action)); return b;
  }
  function minor(id) {
    const value=$(id).value.trim();
    if(!/^\d+(\.\d{1,2})?$/.test(value)) throw new Error('金额请填写非负数字，最多两位小数。');
    const [whole, fraction='']=value.split('.');
    return Number(whole)*100+Number(fraction.padEnd(2,'0'));
  }
  $('goal-form').addEventListener('submit', event => {
    event.preventDefault(); busy($('goal-save'), async () => {
      await post('save', {expected_revision:formRevision || 0,profile:{
        title:$('goal-name').value.trim(),user_reason:$('goal-reason').value.trim(),priority_reason:$('goal-priority').value.trim(),
        preferred_amount_minor:minor('goal-amount'),min_amount_minor:minor('goal-minimum'),
        preferred_date:$('goal-date').value,latest_date:$('goal-latest').value,reserve_floor_minor:minor('goal-reserve')}});
      $('goal-editor').open=false; await refresh();
    });
  });
  $('goal-compare').addEventListener('click', () => busy($('goal-compare'), async () => {
    await post('compare',{expected_revision:current.goal.revision}); await refresh();
  }));
  function optionCard(item) {
    const card=element('article','goal-option'), f=item.forecast;
    card.append(element('h3','',item.label), element('p','goal-price',money(item.amount_minor)),
      element('p','',`计划日期 ${item.due_date}`),
      element('p',item.status==='NEEDS_TRADEOFF'?'warning':'',{
        NEEDS_TRADEOFF:'需要取舍：未满足保留余额',CONDITIONAL:'有条件可行：依赖未来收入',
        WITHIN_KNOWN_CONSTRAINTS:'按已知安排满足保留余额'}[item.status]),
      element('p','',`含预计收入最低 ${money(f.lowest_balance_minor)} · ${f.lowest_balance_date}`),
      element('p','',`收入未到账最低 ${money(f.without_future_income_lowest_balance_minor)} · ${f.without_future_income_lowest_balance_date}`),
      element('p','caption',`保留余额不足：${money(item.reserve_shortfall_minor)}；无未来收入时 ${money(item.without_income_reserve_shortfall_minor)}`));
    if(f.income_conditions.length) card.append(element('p','caption','尚未到账的收入：'+f.income_conditions.map(c=>`${c.title} ${money(c.amount_minor)} / ${c.due_date}`).join('；')));
    const controls=element('div','operation-buttons');
    const select=wait=>async()=>{
      await post('select',{expected_revision:item.revision,assessment_id:item.assessment_id,option_id:item.option_id,
        wait_for_income:wait,idempotency_key:crypto.randomUUID()}); await refresh();
    };
    const choose=button('选择此方案',select(false)); choose.disabled=item.unavailable; controls.append(choose);
    if(f.income_conditions.length){const wait=button('选择并等收入到账',select(true));wait.disabled=item.unavailable;controls.append(wait);}
    card.append(controls,details(f,'查看逐日计算与依据'));
    return card;
  }
  function choiceCard(choice) {
    const card=element('article','goal-followup');
    const option=choice.assessment.options.find(o=>o.option_id===choice.option_id);
    if(choice.display_state==='PLAN_CHANGED') {
      const plan=choice.current_arrangement;
      card.append(element('h3','','已保存安排后来有修改'),
        element('p','',`当前：${plan.title} · ${money(plan.amount_minor)} · ${plan.due_date} · ${{active:'生效中',cancelled:'已取消',completed:'已完成'}[plan.status]} · v${plan.version}`),
        element('p','caption','上面的重点目标是原偏好；原选择与旧回执不代表当前计划。重新比较只生成候选，不会恢复或覆盖当前安排。'),
        details(choice.operation.receipt,'查看历史保存回执'));
      return card;
    }
    card.append(element('h3','',states[choice.display_state] || choice.display_state),
      element('p','',`已选：${option.label} · ${money(option.amount_minor)} · ${option.due_date}`));
    if(choice.wait_for_income){
      card.append(element('p','warning','等待条件：'+option.forecast.income_conditions.map(c=>`${c.title} ${money(c.amount_minor)}（预计 ${c.due_date}）`).join('；')),
        element('p','caption','实际到账并核对关联后，请重新比较选择。当前不自动保存安排，也不自动付款。'));
    } else if(!choice.operation_id && !choice.basis_stale){
      card.append(button('生成安排确认卡',async()=>{await post('prepare',{choice_id:choice.choice_id});await refresh();}));
    }
    card.append(button('核对原回执并更新目标',async()=>{await post('reconcile',{choice_id:choice.choice_id});await refresh();}));
    if(choice.basis_stale && choice.display_state==='WAITING_CONFIRMATION') card.append(element('p','warning','资金依据已变化，请先拒绝旧确认卡，再重新比较。'));
    if(choice.operation?.receipt) card.append(details(choice.operation.receipt,'查看原银行回执'));
    return card;
  }
  function render(state,scope) {
    current=state.competition?.goals || null;
    $('goal-panel').hidden=!current?.enabled || scope!=='bank';
    if(!current?.enabled)return;
    const followup=current.followup;
    const aiReady=followup?.ai_enabled && (!state.model?.managed || state.model.ready_for_requests);
    const followupVersion=JSON.stringify({followup,aiReady,revision:current.goal?.revision,drafts:current.drafts});
    $('goal-panel').querySelector('.panel-heading .tag').textContent=followup ?
      (aiReady?'目标工具与AI检查已启用':'目标工具已接入 · AI当前不可用'):'确定性计算 · 目标AI未启用';
    if(!followup)followupBox.replaceChildren();
    if(followup && followupBox.sourceVersion!==followupVersion){
      const expanded=new Set([...followupBox.querySelectorAll('details[open]')].map(node=>node.dataset.sectionKey));
      followupBox.replaceChildren();
      const disclosure=(title,key)=>{
        const node=element('details','goal-disclosure');node.dataset.sectionKey=key;node.open=expanded.has(key);
        node.append(element('summary','',title));return node;
      };
      const check=followup.check;
      followupBox.append(element('h3','','持续检查'),element('p',aiReady?'caption':'warning',
        aiReady?'AI解释已启用；以下计算始终由工具完成。':'AI当前不可用：确定性计算与变化检查仍保留，模型状态和恢复方式见页面顶部。'));
      const health=followup.service_health;
      if(health?.pending_check_id && !['PENDING','RUNNING','NO_MODEL'].includes(health.state)){
        const warning=element('div','goal-service-health'); warning.setAttribute('role','status');
        warning.append(element('p','warning',health.state==='RECOVERY_EXHAUSTED'?
          '上次分析仍未完成，本次重新分析也未成功。失败记录已保留，需要检查服务原因。':
          '上次分析尚未完成。即使本次没有重要变化，也不表示那次分析已经完成。'));
        if(health.can_retry) {
          const retry=button('重新分析',async()=>{
            await api('/api/competition/goal-followup/retry',{expected_check_id:health.last_attempt_check_id});
            await refresh();
          });
          retry.disabled=!aiReady; warning.append(retry);
        }
        warning.append(element('p','caption','重新分析只读取最新资料，不会重复付款或修改安排。'));
        followupBox.append(warning);
      }
      if(check){
        const labels={NO_MODEL:'已计算，AI未启用',QUIET:'已重新检查，无需新增提醒',PENDING:'等待AI解释',RUNNING:'AI正在读取工具',
          ACCEPTED:'解释已核对字段与当前版本',FAILED:'解释未通过或未完成',INTERRUPTED:'上次解释中断，未自动重试',
          STALE:'旧解释已失效',OFFLINE_CONTRACT_ONLY:'仅离线契约验证，非真实AI解释'};
        followupBox.append(element('p','caption',`${check.context.data.business_date} · ${labels[check.state] || check.state}${followup.stale?' · 依据已变化，等待重新检查':''}`));
        const calculationBox=followup.stale?disclosure('历史计算（已失效，不作当前依据）','stale-calculation'):followupBox;
        if(followup.stale)followupBox.append(calculationBox);
        calculationBox.append(element('h4','',followup.stale?'当时计算，非当前安排':'最近一次计算'));
        const chosen=check.context.data.current_plan;
        const mainPaths=new Set(['status','date_review','coverage',...(chosen?['current_plan','current_plan.primary','current_plan.without_income']:
          ['options.original.primary','options.original.without_income'])]);
        for(const item of check.context.claim_catalog.filter(c=>mainPaths.has(c.path)))
          calculationBox.append(element('p','',item.display));
        const alternatives=check.context.claim_catalog.filter(c=>!mainPaths.has(c.path)&&c.path.startsWith('options.'));
        if(alternatives.length){
          const options=disclosure('查看其他候选试算','alternatives');
          for(const item of alternatives)options.append(element('p','',item.display));
          calculationBox.append(options);
        }
        if(check.state==='ACCEPTED'&&!followup.stale&&check.result?.ok)
          followupBox.append(element('h4','','本次 AI 提醒'),element('p','goal-explanation',check.result.text));
        else if(check.result?.error)followupBox.append(element('p','warning',check.result.error));
      }else followupBox.append(element('p','caption','保存目标后，现有检查服务会使用已确认资料自动计算。'));
      const history=followup.last_explanation;
      if(history && (history.check_id!==check?.check_id || followup.stale)){
        const previous=disclosure('上次 AI 提醒（历史记录）','history:'+history.check_id);
        const context=!history.same_goal_revision?'目标已修改，这条提醒对应旧目标版本。':
          !history.basis_matches_current?'账本或安排已变化，这条提醒对应当时的数据。':'这是此前已完成的提醒。';
        previous.append(element('p','caption',`${history.business_date} · ${context}历史金额仅供回看，最新计算见上方。`),
          element('p','goal-explanation',history.text));
        followupBox.append(previous);
      }
      for(const draft of current.drafts || []){
        const card=element('article','goal-option');
        card.append(element('h3','','AI目标草案（尚未确认）'),element('p','',draft.profile.title),
          element('p','caption',`期望 ${money(draft.profile.preferred_amount_minor)} · ${draft.profile.preferred_date}；采用后仍需核对完整表单。`));
        const adopt=button('采用草案到表单',async()=>{
          const p=draft.profile;
          for(const [id,key] of [['goal-name','title'],['goal-reason','user_reason'],['goal-priority','priority_reason'],['goal-date','preferred_date'],['goal-latest','latest_date']])$(id).value=p[key];
          for(const [id,key] of [['goal-amount','preferred_amount_minor'],['goal-minimum','min_amount_minor'],['goal-reserve','reserve_floor_minor']])$(id).value=(p[key]/100).toFixed(2);
          $('goal-editor').open=true;$('goal-name').focus();
        });
        adopt.disabled=draft.expected_revision!==(current.goal?.revision||0);
        card.append(adopt);followupBox.append(card);
      }
      followupBox.sourceVersion=followupVersion;
    }
    const goal=current.goal, a=current.assessment;
    optionHistory.hidden=!a;
    optionHistoryTitle.textContent=a?.stale?'历史候选方案（已失效，仅供回看）':'候选方案';
    if(historyStale!==Boolean(a?.stale)){historyStale=Boolean(a?.stale);optionHistory.open=!historyStale;}
    $('goal-summary').textContent=goal ? `重点目标偏好：${goal.profile.title} · ${goal.profile.user_reason}。取舍：${goal.profile.priority_reason}。至少保留 ${money(goal.profile.reserve_floor_minor)}。已保存安排以“我的计划”最新记录为准。` : '先明确一件重要的事，再看看现有资金与安排能否支持它。';
    $('goal-compare').disabled=!goal;
    if(formRevision!==(goal?.revision || 0)){
      formRevision=goal?.revision || 0;
      if(goal){const p=goal.profile;
        for(const [id,key] of [['goal-name','title'],['goal-reason','user_reason'],['goal-priority','priority_reason'],['goal-date','preferred_date'],['goal-latest','latest_date']]) $(id).value=p[key];
        for(const [id,key] of [['goal-amount','preferred_amount_minor'],['goal-minimum','min_amount_minor'],['goal-reserve','reserve_floor_minor']]) $(id).value=(p[key]/100).toFixed(2);
      } else $('goal-editor').open=true;
    }
    $('goal-date').min=current.business_date; $('goal-latest').min=current.business_date;
    $('goal-basis').textContent=a ? `${a.window_start} 至 ${a.window_end}，全部候选使用同一份账本。${a.stale?'依据已变化，旧方案仅供回看，请重新计算。':'只比较下面候选，不代表所有可能方案。'} 其他安排全部保留，包括未关联的旧目标。` : '保存后点击比较；本区由代码计算，不调用 AI。';
    stableCards($('goal-options'),(a?.options || []).map(o=>({...o,assessment_id:a.assessment_id,revision:a.goal_revision,
      unavailable:a.stale || current.choice?.assessment_id===a.assessment_id})),o=>o.option_id,optionCard);
    const fulfillment=current.fulfillment;
    stableCards($('goal-choice'),current.choice?[current.choice]:[],c=>c.choice_id,choiceCard);
    $('goal-choice').querySelector('.goal-fulfillment')?.remove();
    if(fulfillment){
      const label=fulfillment.status==='EXPENSE_MATCHED'?'目标对应支出已确认；未重复记账，不代表现实目标已经达成。':
        `关联安排状态：${{active:'有效',completed:'手动标记完成，未核对实际支出',cancelled:'已取消'}[fulfillment.arrangement?.status] || '待核对'}。`;
      $('goal-choice').append(element('p','goal-fulfillment caption',label));
    }
  }
  return {render};
}
