/* Manual plan workflow uses the bank's one arrangement ledger and confirmation UI. */
export function createPlanWorkspace({api, refresh, openConfirm, element, money}) {
  const $ = id => document.getElementById(id);
  const kinds = {goal:'生活目标',expense:'应付支出',income:'预计收入'};
  const statuses = {active:'生效中',completed:'已完成',cancelled:'已取消'};
  let state=null, editing=null, status='active', preview=null, pending=null, attempt=null, basis=null;
  const post=(action,body)=>api(`/api/competition/plans/${action}`,body);
  const formControls=()=>[...$('plan-form').querySelectorAll('input,select')];
  function button(text,callback) {
    const b=element('button','secondary',text);b.type='button';
    b.addEventListener('click',()=>run(b,callback));return b;
  }
  async function run(b,callback) {
    if(b.disabled)return;b.disabled=true;$('plans-error').hidden=true;
    try {await callback();} catch(error) {
      $('plans-error').textContent=error.message;$('plans-error').hidden=false;
      $('plans-error').scrollIntoView({block:'nearest'});
    } finally {b.disabled=false;}
  }
  function amount(id) {
    const value=$(id).value.trim();
    if(!/^\d+(\.\d{1,2})?$/.test(value))throw new Error('金额请填数字，最多两位小数。');
    const [whole,fraction='']=value.split('.'), minor=Number(whole)*100+Number(fraction.padEnd(2,'0'));
    if(!Number.isSafeInteger(minor)||minor<=0||minor>100000000000)throw new Error('请填写有效的正数金额。');
    return minor;
  }
  function draft() {
    return {title:$('plan-title').value.trim(),kind:$('plan-kind').value,amount_minor:amount('plan-amount'),
      due_date:$('plan-date').value,status,...(editing?{arrangement_id:editing.arrangement_id,expected_version:editing.version}:{})};
  }
  function lock(value) {formControls().forEach(e=>e.disabled=value||status==='cancelled');$('plan-preview').hidden=value;$('plan-discard').hidden=value;}
  function invalidate(message='输入已变化，请重新预览。') {
    if(pending)return;
    preview=null;attempt=null;$('plan-pending-actions').hidden=true;
    if($('plan-comparison').childElementCount)$('plan-comparison').replaceChildren(element('p','warning',message));
  }
  function start(record=null,nextStatus='active') {
    if(pending)throw new Error('请先继续确认或放弃当前草案，不能重复生成安排。');
    editing=record?structuredClone(record):null;status=nextStatus;preview=null;attempt=null;
    $('plan-editor').hidden=false;$('plan-comparison').replaceChildren();$('plan-pending-actions').hidden=true;
    $('plan-editor-title').textContent=nextStatus==='cancelled'?'取消计划':record?'修改计划':'新增计划';
    $('plan-edit-version').textContent=record?`正在编辑 v${record.version}；保存时会重新核对最新版本。`:'填写后先看资金影响，再独立确认保存。';
    $('plan-title').value=record?.title||'';$('plan-kind').value=record?.kind||'goal';
    $('plan-amount').value=record?(record.amount_minor/100).toFixed(2):'';
    $('plan-date').value=record?.due_date||state.business_date;$('plan-date').min=nextStatus==='cancelled'?'':state.business_date;
    $('plan-preview').textContent=nextStatus==='cancelled'?'预览取消后的影响':'预览资金影响';
    $('plan-prepare').textContent='核对并生成确认卡';$('plan-reject').hidden=true;lock(false);
    $('plan-editor').scrollIntoView({block:'start'});
    if(nextStatus==='active')$('plan-title').focus();
  }
  function forecastBox(f,label) {
    const box=element('div','plan-forecast');box.append(element('h4','',label),
      element('p','',`含预计收入：最低 ${money(f.lowest_balance_minor)} · ${f.lowest_balance_date}；现金缺口（跌破0）${money(f.gap_minor)}`),
      element('p','',`不计未来收入：最低 ${money(f.without_future_income_lowest_balance_minor)} · ${f.without_future_income_lowest_balance_date}；现金缺口（跌破0）${money(f.without_future_income_gap_minor)}`));
    const r=f.reserve_assessment;
    if(r) box.append(element('p','',`参考目标“${r.title}” v${r.revision}：希望保留 ${money(r.floor_minor)}；含预计收入时不足 ${money(r.shortfall_minor)}，不计未来收入时不足 ${money(r.without_income_shortfall_minor)}。这不是全局底线，也不代表其他计划均可负担。`));
    else box.append(element('p','caption','未设置重点目标的保留底线，尚不能判断希望留下的钱是否足够。'));
    return box;
  }
  function coverage(f) {
    const box=element('div','caption');box.append(element('p','',`计算范围：${f.as_of_date} 至 ${f.through_date}。仅按已记录的一次性安排，未登记的日常生活开销未知，不按0元视为完整预算；合计账户预测不保证某个付款账户够用。`));
    const omitted=f.plan_coverage?.outside_window||[];
    if(omitted.length)box.append(element('p','warning','以下计划未纳入这个窗口，尚不能判断其资金影响：'+omitted.map(a=>`${a.title}（${a.due_date}）`).join('；')));
    if(f.income_conditions.length)box.append(element('p','','假设到账的收入：'+f.income_conditions.map(a=>`${a.title} · ${a.due_date} · ${money(a.amount_minor)}`).join('；')+'。均未到账。'));
    for(const warning of f.reconciliation_warnings||[])box.append(element('p','warning',warning));
    return box;
  }
  function comparison(data,container,labels=['更改前','更改后']) {
    container.replaceChildren(coverage(data.after),forecastBox(data.before,labels[0]),forecastBox(data.after,labels[1]),element('p','caption',data.notice));
    if(data.after.gap_minor||data.after.without_future_income_gap_minor)container.append(element('p','warning','结论：预测会出现现金缺口。下一步：降低金额或调整日期后重新比较；仍可记录计划，但不代表可负担。'));
    else if(data.after.reserve_assessment?.shortfall_minor||data.after.reserve_assessment?.without_income_shortfall_minor)container.append(element('p','warning','结论：尚未跌破0，但达不到上述目标的保留要求。下一步：核对预计收入，或调整金额/日期后再比较。'));
    else container.append(element('p','caption','结论：已记录范围内未见现金缺口。下一步：补齐生活开销并核对收入到账；未设置底线时，仍不能判断保留资金是否足够。'));
  }
  function list(records) {
    const nodes=records.map(record=>{
      const card=element('article','plan-row');card.append(element('h3','',record.title),
        element('p','',`${money(record.amount_minor)} · ${record.due_date}`),
        element('p','caption',`${kinds[record.kind]} · ${statuses[record.status]} · v${record.version}`));
      if(record.status==='active') {
        const controls=element('div','operation-buttons');
        controls.append(button('修改',async()=>start(record)),button('取消计划',async()=>start(record,'cancelled')));card.append(controls);
      }
      return card;
    });
    $('plans-list').replaceChildren(...(nodes.length?nodes:[element('p','','还没有已确认计划。可以先新增一条。')]));
    $('plans-notice').textContent=`共 ${records.length} 条已确认安排，其中 ${records.filter(a=>a.status==='active').length} 条生效中。`;
  }
  async function load() {
    const data=await api('/api/competition/plans');state=data;list(data.arrangements);
    $('plans-overview').replaceChildren(coverage(data.forecast),forecastBox(data.forecast,'全部有效计划'));
    $('plan-spend-date').min=data.business_date;$('plan-spend-date').max=data.forecast.plan_coverage.maximum_date;
    if(!$('plan-spend-date').value)$('plan-spend-date').value=data.business_date;
  }
  function render(bankState) {
    const next=JSON.stringify([bankState.business_date,bankState.accounts,bankState.arrangements,bankState.competition?.goals?.goal]);
    if(basis && basis!==next) {
      if(pending)$('plan-comparison').prepend(element('p','warning','参考目标或账本已变化，以下是旧版本预览。请放弃草案后重新比较；确认卡仅授权所列计划修改，不保证满足当前保留要求。'));
      invalidate('余额、计划、目标偏好或业务日期已变化。输入已保留，请刷新列表并重新预览；版本冲突时重新选择记录。');
      $('plan-spend-result').replaceChildren(element('p','caption','依据已变化，请重新试算。'));
      $('plans-overview').replaceChildren(element('p','caption','依据已变化，请刷新列表与资金影响。'));
      if(state){state.arrangements=bankState.arrangements;state.business_date=bankState.business_date;list(state.arrangements);}
    }
    basis=next;
  }
  async function confirmed(operation) {
    if(pending?.operation_id===operation.operation_id){pending=null;preview=null;attempt=null;editing=null;$('plan-editor').hidden=true;}
    if($('plans-dialog').open)await load();
  }
  $('plans-open').addEventListener('click',()=>{ $('plans-dialog').showModal();run($('plans-refresh'),load); });
  $('plans-refresh').addEventListener('click',()=>run($('plans-refresh'),async()=>{await refresh();await load();}));
  $('plan-new').addEventListener('click',()=>run($('plan-new'),async()=>{if(!state)await load();start();}));
  $('plan-discard').addEventListener('click',()=>{editing=null;preview=null;attempt=null;$('plan-editor').hidden=true;});
  $('plan-form').addEventListener('input',()=>invalidate());
  $('plan-form').addEventListener('change',()=>invalidate());
  $('plan-form').addEventListener('submit',event=>{event.preventDefault();run($('plan-preview'),async()=>{
    const data=await post('preview',draft());preview=data;attempt=crypto.randomUUID();comparison(data,$('plan-comparison'));
    const describe=a=>a?`${a.title} · ${kinds[a.kind]} · ${money(a.amount_minor)} · ${a.due_date} · ${statuses[a.status]}`:'无（新增）';
    $('plan-comparison').prepend(element('p','',`原记录：${describe(data.original)}`),element('p','',`拟保存：${describe(data.draft)}`));
    $('plan-pending-actions').hidden=false;$('plan-prepare').textContent='核对并生成确认卡';$('plan-reject').hidden=true;
  });});
  $('plan-prepare').addEventListener('click',()=>run($('plan-prepare'),async()=>{
    if(!pending){if(!preview)throw new Error('请先重新预览。');pending=await post('prepare',{draft:preview.draft,preview_token:preview.preview_token,idempotency_key:attempt});lock(true);}
    $('plan-prepare').textContent='继续核对确认';$('plan-reject').hidden=false;openConfirm(pending);await refresh();
  }));
  $('plan-reject').addEventListener('click',()=>run($('plan-reject'),async()=>{
    await api('/api/actions/reject',{operation_id:pending.operation_id});pending=null;lock(false);invalidate('草案已放弃，计划未保存。修改后重新预览。');await refresh();
  }));
  $('plan-spending-form').addEventListener('submit',event=>{event.preventDefault();run($('plan-spend-submit'),async()=>{
    const result=await post('spending',{amount_minor:amount('plan-spend-amount'),due_date:$('plan-spend-date').value});
    comparison(result,$('plan-spend-result'),['不花这笔钱','花掉这笔钱']);
  });});
  $('plan-spending-form').addEventListener('input',()=>{
    if($('plan-spend-result').childElementCount)$('plan-spend-result').replaceChildren(element('p','caption','支出条件已变化，请重新试算。'));
  });
  return {render,confirmed};
}
