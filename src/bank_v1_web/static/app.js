'use strict';
import {createHistoryWorkspace} from './history.js';
import {createCompetitionWorkspace} from './competition.js';
import {createGoalWorkspace} from './goals.js';
import {renderRuntimeStatus} from './runtime.js';

const ui = {state: null, csrf: '', messageVersion: '', selectedOperation: null, eventAttempt: null, loading: false, scope: 'bank', historyLoaded: false, historyOffset: 0};
const $ = id => document.getElementById(id);
const money = minor => new Intl.NumberFormat('zh-CN', {style: 'currency', currency: 'CNY'}).format((minor || 0) / 100);
const names = {TRANSFER: '账户转账', transfer: '账户转账', ARRANGEMENT: '更新安排', arrangement: '更新安排', MANDATE_QUERY: '查询自动扣款', mandate_query: '查询自动扣款', MANDATE_CANCEL: '取消扣款授权', mandate_cancel: '取消扣款授权', expense: '消费支出', income: '收入到账', refund: '消费退款', due_check: '定期检查', arrangement_changed: '安排更新', PENDING: '等待分析', FAILED: '分析失败', DONE: '分析完成', SUCCEEDED: '已完成', REJECTED: '已拒绝', active: '进行中', completed: '已完成', cancelled: '已取消', goal: '目标', STRONG: '需模拟强验证', YELLOW: '需本人确认'};
const label = value => names[value] || value;
names.arrangement_match = '关联流水与安排';
names.TRANSFER_IN = '本人账户转入';
names.TRANSFER_OUT = '本人账户转出';
names.EXPIRED = '已过期';
names.CANCELLED = '已取消';
names.ACTIVE = '生效中';

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
  const dialog = document.querySelector('dialog[open]');
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
function renderOverview(state) {
  $('total').textContent = money(state.totals.reduce((sum, item) => sum + item.balance_minor, 0));
  $('accounts').replaceChildren(...state.accounts.map(account => {
    const node = element('div', '', `${account.name} ${account.account_id}`);
    node.append(element('strong', '', money(account.balance_minor)));
    return node;
  }));
  const forecast = state.forecast.data;
  $('forecast-value').textContent = forecast.gap_minor > 0 ? `预计最大缺口 ${money(forecast.gap_minor)}` : `预计最低余额 ${money(forecast.lowest_balance_minor)}`;
  $('forecast-value').className = 'forecast-number' + (forecast.gap_minor > 0 ? ' risk' : '');
  $('forecast-date').textContent = forecast.lowest_balance_date || '';
  $('conservative').textContent = (forecast.without_future_income_gap_minor > 0 ? `如果预计收入未到账：负余额缺口 ${money(forecast.without_future_income_gap_minor)}` : '如果预计收入未到账：未预测到负余额')
    + (state.competition?.goals?.goal ? '。目标要求的保留余额另见下方持续检查。' : '。仅包含已记录安排。');
  $('forecast-warning').textContent = (forecast.reconciliation_warnings || []).join(' ');
  $('forecast-warning').hidden = !(forecast.reconciliation_warnings || []).length;
  const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
  svg.setAttribute('viewBox', '0 0 320 58');
  svg.setAttribute('role', 'img');
  svg.setAttribute('aria-label', `未来30天预计最低余额${money(forecast.lowest_balance_minor)}。未来收入未到账时缺口${money(forecast.without_future_income_gap_minor)}。`);
  const values = forecast.daily.flatMap(day => [day.projected_balance_minor, day.without_future_income_minor]);
  const min = Math.min(0, ...values), max = Math.max(1, ...values), scale = max - min;
  for (const [field, color, dashed] of [['projected_balance_minor', '#28624b', false], ['without_future_income_minor', '#9aab8f', true]]) {
    const line = document.createElementNS(svg.namespaceURI, 'polyline');
    line.setAttribute('points', forecast.daily.map((day, index) => `${index / Math.max(1, forecast.daily.length - 1) * 316 + 2},${54 - (day[field] - min) / scale * 48}`).join(' '));
    line.setAttribute('fill', 'none'); line.setAttribute('stroke', color); line.setAttribute('stroke-width', '2');
    if (dashed) line.setAttribute('stroke-dasharray', '4 4');
    svg.append(line);
  }
  $('forecast-chart').replaceChildren(svg);
  if (document.activeElement !== $('cadence')) $('cadence').value = state.schedule.enabled ? state.schedule.cadence : 'off';
  $('business-date').textContent = `业务日期 ${state.business_date} · 北京时间`;
}
function renderArrangements(state) {
  $('arrangements').replaceChildren(...state.arrangements.map(item => {
    const node = element('div', 'arrangement');
    const text = element('div', 'arrangement-title', item.title);
    const kind = {income: '预计收入', expense: '应付支出', goal: '目标'}[item.kind];
    text.append(element('small', '', `${item.due_date} · ${kind} · ${label(item.status || 'active')} · v${item.version}`));
    node.append(text, element('div', 'arrangement-amount', `${item.kind === 'income' ? '+' : ''}${money(item.amount_minor)}`));
    return node;
  }));
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
function renderOperations(state) {
  const now = Date.now();
  const operations = state.operations.map(item => ({...item, is_expired: new Date(item.expires_at).getTime() <= now}));
  const pending = operations.filter(item => item.status === 'PENDING' && !item.is_expired);
  $('pending-count').textContent = pending.length;
  stableCards($('operations'), pending, item => item.operation_id, item => {
    const node = element('article', 'operation');
    node.append(element('h3', '', label(item.kind)), element('p', '', operationLines(item).join('\n')));
    const expired = item.is_expired;
    node.append(element('p', 'caption', expired ? '确认已过期，请重新提出需求。' : (item.required_verification === 'STRONG' && state.security?.mode === 'independent' ? '需要独立口令与认证器验证' : label(item.required_verification))));
    const buttons = element('div', 'operation-buttons');
    const confirm = element('button', 'primary', '核对并确认');
    confirm.disabled = expired;
    confirm.addEventListener('click', () => openConfirm(item));
    const reject = element('button', 'secondary', '拒绝');
    reject.addEventListener('click', () => busy(reject, async () => { await api('/api/actions/reject', {operation_id: item.operation_id}); await refresh(); }));
    buttons.append(confirm, reject); node.append(buttons); return node;
  });
  if (!pending.length) $('operations').append(element('p', 'empty', '暂时没有待确认操作。需要时，我会把明细放在这里。'));
  const receipts = operations.filter(item => item.status !== 'PENDING' || item.is_expired);
  stableCards($('receipts'), receipts, item => item.operation_id, operationReceipt);
  if (!receipts.length) $('receipts').append(element('p', 'empty', '操作完成或拒绝后，回执会保存在这里。'));
}
function renderMessages(state) {
  const jobs = state.jobs.filter(job => job.kind === 'chat' && (job.scope || 'bank') === ui.scope);
  const version = ui.scope + jobs.map(job => job.id + job.status).join(',');
  const running = state.jobs.find(job => ['QUEUED', 'RUNNING'].includes(job.status) && (job.scope || 'bank') === ui.scope);
  $('agent-status').textContent = running ? (running.kind === 'event' ? '正在主动检查' : '正在分析') : (state.worker_error ? '后台需检查' : '可以开始');
  if (version === ui.messageVersion) return;
  ui.messageVersion = version;
  const container = $('conversation');
  const nearBottom = container.scrollHeight - container.scrollTop - container.clientHeight < 130;
  const expanded = new Map([...container.querySelectorAll('.chat-pair')].map(node => [node.dataset.jobId, node.querySelector('details')?.open]));
  container.querySelectorAll('.chat-pair').forEach(node => node.remove());
  for (const job of jobs) {
    const node = element('article', 'chat-pair');
    node.dataset.jobId = job.id;
    node.append(element('div', 'chat-user', job.retry_of ? '重新查询上次问题，仅核对现有结果。' : job.prompt));
    if (job.status === 'DONE') {
      node.append(answerBody(job.result.text));
      const trace = job.result.trace?.filter(item => item.kind === 'response') || [];
      if (trace.length) { const evidence = details(trace, `查看本次 ${trace.length} 次工具调用依据`); evidence.className = 'trace-detail'; evidence.open = expanded.get(job.id) || false; node.append(evidence); }
    } else if (['FAILED', 'INTERRUPTED'].includes(job.status)) {
      const error = job.result?.error;
      const message = job.result?.error_message || (typeof error === 'string' ? error : error?.message) || '这次分析未完成。已入账的收支仍有效，请查看待确认操作及回执。';
      const failure = element('div', 'chat-failed', message);
      failure.setAttribute('role', 'alert'); node.append(failure);
      const retry = element('button', 'secondary', '重新查询（不重复办理）');
      const recovered = state.jobs.some(item => item.retry_of === job.id);
      retry.disabled = recovered;
      if (recovered) retry.textContent = '已创建恢复查询，见后续记录';
      retry.addEventListener('click', () => busy(retry, async () => { await api(`/api/jobs/${job.id}/retry`, {}); await refresh(); }));
      node.append(retry);
    } else node.append(element('div', 'chat-pending', job.status === 'RUNNING' ? '正在读取所选资料并分析，可能需要一两分钟…' : '已排队，账本与历史仍可查看…'));
    node.append(element('div', 'chat-meta', new Date(job.created_at).toLocaleString('zh-CN'))); container.append(node);
  }
  if (nearBottom || jobs.at(-1)?.status === 'QUEUED') container.scrollTop = container.scrollHeight;
}
function renderEvents(state) {
  $('event-count').textContent = `${state.events.length} 次`;
  const events = state.events.slice(0, 8);
  const rows = events.map(event => ({...event, notification: state.notifications.find(item => item.event_id === event.event_id),
    transaction: state.transactions.find(item => item.transaction_id === event.payload.transaction_id),
    operation: state.operations.find(item => item.operation_id === event.payload.operation_id), facts: state.event_facts?.[event.event_id],
    analyzing: state.jobs.some(job => job.status === 'RUNNING' && job.event_key?.split(':')[1] === event.event_id),
    goalDelegated: state.competition?.goals?.followup?.event_handling==='GOAL_FOLLOWUP'}));
  stableCards($('events'), rows, event => event.event_id, event => {
    const node = element('article', 'event-item');
    const heading = element('div', 'event-heading');
    heading.append(element('strong', '', label(event.kind)), element('span', 'tag', event.analyzing ? '正在分析' : label(event.state)));
    node.append(heading);
    node.append(element('p', 'caption', `事件时间 ${new Date(event.created_at).toLocaleString('zh-CN')} · 以下为该次检查记录，当前余额见顶部`));
    const {transaction, operation} = event;
    if (transaction) node.append(element('p', 'bank-fact', `已入账 ${money(transaction.amount_minor)}，无需等待分析完成。`));
    else if (operation) node.append(element('p', 'bank-fact', `银行操作：${label(operation.status)}。具体结果以操作回执为准。`));
    const notification = event.notification;
    if (notification) node.append(element('p', 'warning', '当次风险记录：' + notification.message));
    if (event.facts?.summary) node.append(element('p', 'bank-fact', '按当前账本重算：' + event.facts.summary));
    if (event.analysis_text) {
      const analysis = element('details', 'event-analysis');
      analysis.append(element('summary', '', '查看当次模型解释'), answerBody(event.analysis_text));
      node.append(analysis);
    } else node.append(element('p', '', event.goalDelegated ? '银行事实已记录；变化统一在「持续检查」中评估，此事件未标成AI解释完成。' : event.error ||
      (state.model?.managed && !state.model.ready_for_requests && !event.analyzing ? '银行事实已保存，当前AI不能受理分析。状态和恢复方式见页面顶部。' :
      event.analyzing ? '模型正在分析，当前确定性计算已显示在上方。' : '已排队。先保存银行事件，再由智能体读取工具分析。')));
    if (event.state === 'FAILED' && event.attempts >= 3) {
      const retry = element('button', 'text-button', '重新分析这条事件 →');
      retry.addEventListener('click', () => busy(retry, async () => { await api('/api/actions/retry_event', {event_id: event.event_id}); await refresh(); }));
      node.append(retry);
    }
    return node;
  });
  if (!events.length) $('events').append(element('p', 'empty', '收支变化或到期检查后，分析结果会留在这里。'));
}
function renderMatches(state) {
  const items = state.match_candidates || [];
  $('match-count').textContent = items.length;
  stableCards($('matches'), items, item => item.candidate_id, item => {
    const node = element('article', 'operation');
    node.append(element('h3', '', item.title), element('p', '', `${item.transaction_date} 已入账 ${money(item.amount_minor)}，可能对应 ${item.due_date} 的安排。`));
    node.append(element('p', 'caption', (item.reasons || []).join('；')));
    node.append(element('p', 'warning', item.ambiguous ? '有多个可能对应的记录，请核对后选择。' : '只是候选，确认后才完成安排并停止重复预测。'));
    const buttons = element('div', 'operation-buttons');
    const prepare = element('button', 'primary', '核对关联');
    prepare.addEventListener('click', () => busy(prepare, async () => {
      const operation = await api('/api/actions/prepare_match', {transaction_id: item.transaction_id, arrangement_id: item.arrangement_id, expected_version: item.expected_version, idempotency_key: crypto.randomUUID()});
      await refresh(); openConfirm(operation);
    }));
    const reject = element('button', 'secondary', '不对应此安排');
    reject.addEventListener('click', () => busy(reject, async () => { await api('/api/actions/reject_match', {transaction_id: item.transaction_id, arrangement_id: item.arrangement_id, expected_version: item.expected_version}); await refresh(); }));
    buttons.append(prepare, reject); node.append(buttons); return node;
  });
  if (!items.length) $('matches').replaceChildren(element('p', 'empty', '暂时没有需要核对的到账与安排。'));
}
function renderTransactions(state) {
  $('transactions').replaceChildren(...state.transactions.slice(0, 30).map(tx => {
    const row = element('tr'), title = element('td', '', tx.description);
    title.append(element('small', '', `${tx.business_date} · ${tx.kind === 'OPENING' ? '期初余额' : label(tx.kind)}`));
    row.append(title, element('td', '', tx.account_id), element('td', 'amount-cell' + (tx.amount_minor > 0 ? ' income' : ''), `${tx.amount_minor > 0 ? '+' : ''}${money(tx.amount_minor)}`));
    return row;
  }));
}
async function refresh() {
  if (ui.loading) return;
  ui.loading = true;
  try {
    const state = await api('/api/state'); ui.state = state; ui.csrf = state.csrf_token;
    renderOverview(state); renderArrangements(state); renderOperations(state); renderMatches(state); renderMessages(state); renderEvents(state); renderTransactions(state);
    competitionWorkspace.render(state, ui.scope);
    goalWorkspace.render(state, ui.scope);
    renderRuntimeStatus(state, document);
  } finally { ui.loading = false; }
}
async function sendPrompt(prompt) {
  if (ui.state?.model?.managed && !ui.state.model.ready_for_requests) throw new Error(ui.state.model.reason);
  await api('/api/chat', {prompt, scope: ui.scope}); $('prompt').value = ''; await refresh();
}
$('chat-form').addEventListener('submit', event => { event.preventDefault(); const prompt = $('prompt').value.trim(); if (prompt) busy($('send'), () => sendPrompt(prompt)); });
$('prompt').addEventListener('keydown', event => { if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) $('chat-form').requestSubmit(); });
document.querySelectorAll('[data-prompt]').forEach(button => button.addEventListener('click', () => busy(button, () => sendPrompt(button.dataset.prompt))));
$('due-check').addEventListener('click', () => busy($('due-check'), async () => { await api('/api/actions/due_check', {}); await refresh(); }));
$('cadence').addEventListener('change', () => busy($('cadence'), async () => { await api('/api/schedule', {enabled: $('cadence').value !== 'off', cadence: $('cadence').value === 'off' ? 'daily' : $('cadence').value}); await refresh(); }));
$('open-simulator').addEventListener('click', () => {
  const expenses = ui.state?.transactions.filter(tx => tx.kind === 'expense' && tx.account_id === 'A') || [];
  $('original-transaction').replaceChildren(...expenses.map(tx => { const option = element('option', '', `${tx.business_date} ${tx.description} ${money(-tx.amount_minor)}`); option.value = tx.transaction_id; return option; }));
  $('simulator').showModal();
});
$('close-simulator').addEventListener('click', () => $('simulator').close());
$('event-kind').addEventListener('change', () => { $('refund-field').hidden = $('event-kind').value !== 'refund'; });
$('event-form').addEventListener('submit', event => {
  event.preventDefault(); busy($('submit-event'), async () => {
    const amount = $('event-amount').value.trim();
    if (!/^\d+(\.\d{1,2})?$/.test(amount)) throw new Error('请填写最多两位小数的金额。');
    const [whole, fraction = ''] = amount.split('.');
    const minor = Number(whole) * 100 + Number(fraction.padEnd(2, '0'));
    if (!Number.isSafeInteger(minor) || minor <= 0) throw new Error('金额必须是有效的正数。');
    const body = {kind: $('event-kind').value, amount_minor: minor, description: $('event-description').value.trim() || '模拟银行交易'};
    if (body.kind === 'refund') body.original_transaction_id = $('original-transaction').value;
    const fingerprint = JSON.stringify(body);
    if (!ui.eventAttempt || ui.eventAttempt.fingerprint !== fingerprint) ui.eventAttempt = {fingerprint, key: crypto.randomUUID()};
    await api('/api/actions/inject_transaction', {...body, idempotency_key: ui.eventAttempt.key});
    ui.eventAttempt = null; $('simulator').close(); $('event-form').reset(); $('refund-field').hidden = true; await refresh();
  });
});
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
  await api('/api/actions/confirm', body); $('confirm-dialog').close(); await refresh();
}));
const historyWorkspace = createHistoryWorkspace({request: api, analyze: prompt => sendPrompt(prompt).catch(showError)});
const competitionWorkspace = createCompetitionWorkspace({api, busy, refresh, element, details, money, stableCards});
const goalWorkspace = createGoalWorkspace({api, busy, refresh, element, details, money, stableCards});
async function selectScope(scope) {
  ui.scope = scope;
  document.querySelectorAll('[data-bank]').forEach(node => { node.hidden = scope !== 'bank'; });
  document.querySelectorAll('[data-history]').forEach(node => { node.hidden = scope !== 'moneydata'; });
  $('bank-tab').setAttribute('aria-pressed', String(scope === 'bank'));
  $('history-tab').setAttribute('aria-pressed', String(scope === 'moneydata'));
  $('workspace').classList.toggle('history-mode', scope === 'moneydata');
  $('scope-note').textContent = scope === 'bank' ? '虚构人民币银行' : 'MoneyData公开历史 · 币种未提供';
  document.querySelector('.composer-bottom > span').textContent = scope === 'bank' ? '分析基于工具读取的账本与已确认安排' : '只读来源历史；币种、当前余额与未来安排尚未知';
  $('prompt').placeholder = scope === 'bank' ? '例如：把电脑购买计划推迟一周，再帮我算算。' : '例如：2022年有哪些主要出入账？';
  $('welcome-text').textContent = scope === 'bank'
    ? '结合账本与已记录安排，看看钱什么时候要用。需要变更时先核对确认。'
    : '当前仅分析MoneyData来源流水。历史数据与模拟银行分开，不能据此确认今天余额或未来安排。';
  if (ui.state) renderMessages(ui.state);
  if (ui.state) competitionWorkspace.render(ui.state, scope);
  if (ui.state) goalWorkspace.render(ui.state, scope);
  if (ui.state) renderRuntimeStatus(ui.state, document);
  if (scope === 'moneydata') await historyWorkspace.show();
}
$('bank-tab').addEventListener('click', () => selectScope('bank').catch(showError));
$('history-tab').addEventListener('click', () => selectScope('moneydata').catch(showError));
refresh().catch(showError);
setInterval(() => refresh().catch(error => { $('agent-status').textContent = '服务暂未连接'; showError(error); }), 3500);
