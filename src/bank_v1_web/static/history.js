// Read-only historical workspace. Source amounts never use the bank's CNY formatter.
export function createHistoryWorkspace({request, analyze}) {
  const get = id => document.getElementById(id);
  const amount = value => value == null ? '—' : new Intl.NumberFormat('zh-CN', {minimumFractionDigits: 2, maximumFractionDigits: 2}).format(value / 100);
  const node = (tag, text) => { const item = document.createElement(tag); if (text !== undefined) item.textContent = text; return item; };
  const state = {loaded: false, offset: 0, total: 0, limit: 50, loading: false};

  function query(offset) {
    const params = new URLSearchParams({offset, limit: state.limit});
    for (const [field, id] of [['start_date', 'history-start'], ['end_date', 'history-end']]) {
      if (get(id).value) params.set(field, get(id).value);
    }
    return params;
  }
  function displayError(error) {
    get('history-error').textContent = error.message || '历史资料暂时无法读取。';
    get('history-error').hidden = false;
  }
  async function loadPage(offset) {
    if (state.loading) return;
    state.loading = true;
    get('history-error').hidden = true;
    const controls = ['history-prev', 'history-next', 'history-last', 'history-apply'];
    controls.forEach(id => { get(id).disabled = true; });
    try {
      const params = query(offset);
      const result = await request('/api/history/transactions?' + params);
      params.delete('offset'); params.delete('limit');
      const summary = await request('/api/history/summary?' + params);
      const data = result.data;
      state.offset = data.offset; state.total = data.total;
      get('history-page').textContent = data.total ? `第 ${data.offset + 1}—${data.offset + data.transactions.length} 条 / 共 ${data.total} 条` : '该期间没有流水';
      get('history-rows').replaceChildren(...data.transactions.map(tx => {
        const row = node('tr');
        for (const value of [tx.transaction_date, tx.description_raw, tx.category_raw || '未分类', amount(tx.debit_minor), amount(tx.credit_minor), amount(tx.balance_minor)]) row.append(node('td', value));
        const evidence = node('td');
        const detail = node('details');
        detail.append(node('summary', '源编号 ' + tx.source_transaction_number), node('p', `原表第 ${tx.source_row} 行；日期变换：${tx.date_transform}`), node('p', `原始日期：${tx.source_date_raw}`), node('p', `源行 SHA-256：${tx.source_row_sha256}`));
        evidence.append(detail); row.append(evidence); return row;
      }));
      const totals = summary.data.totals;
      get('history-totals').textContent = `${totals.count} 条 · 出账 ${amount(totals.debit_minor)} · 入账 ${amount(totals.credit_minor)} · 净变动 ${amount(totals.net_change_minor)}（原数据金额单位，币种未提供）`;
      const balance = summary.data.balance_reconciliation;
      get('history-equation').textContent = balance.available
        ? `期间对账：推导期初 ${amount(balance.opening_minor)} ＋ 入账 ${amount(totals.credit_minor)} − 出账 ${amount(totals.debit_minor)} ＝ 期末 ${amount(balance.closing_minor)}；${balance.equation_matches ? '相符' : '不相符，需检查'}。期初由最早一笔前推，不是原表单独提供的余额。`
        : '此区间没有交易，无法据此推导期初期末余额。';
      get('history-groups').replaceChildren(...summary.data.groups.sort((a, b) => b.debit_minor - a.debit_minor).slice(0, 8).map(group => {
        const item = node('div'); item.className = 'history-category';
        item.append(node('span', group.key || '未分类'), node('strong', amount(group.debit_minor)), node('small', `${group.count} 条`));
        return item;
      }));
      get('history-evidence').textContent = `本次汇总依据：${summary.evidence_id}`;
    } catch (error) { displayError(error); }
    finally {
      state.loading = false;
      get('history-prev').disabled = state.offset === 0;
      get('history-next').disabled = state.offset + state.limit >= state.total;
      get('history-last').disabled = state.offset + state.limit >= state.total;
      get('history-apply').disabled = false;
    }
  }
  async function show() {
    if (state.loaded) return;
    try {
      const result = await request('/api/history/overview');
      const manifest = result.data.manifest;
      get('history-range').textContent = `${manifest.date_min} 至 ${manifest.date_max} · ${manifest.row_count} 条完整来源流水`;
      get('history-provenance').textContent = JSON.stringify(result.data, null, 2);
      get('history-limits').textContent = result.data.forecast_readiness.explanation;
      get('history-start').value = manifest.date_min;
      get('history-end').value = manifest.date_max;
      state.loaded = true;
      await loadPage(0);
    } catch (error) { displayError(error); }
  }
  get('history-prev').addEventListener('click', () => loadPage(Math.max(0, state.offset - state.limit)));
  get('history-next').addEventListener('click', () => loadPage(state.offset + state.limit));
  get('history-last').addEventListener('click', () => loadPage(Math.floor(Math.max(0, state.total - 1) / state.limit) * state.limit));
  get('history-filter').addEventListener('submit', event => { event.preventDefault(); loadPage(0); });
  get('history-analyze').addEventListener('click', () => analyze(`分析MoneyData完整历史：读取来源概览和完整期间的分类汇总即可，不需要逐月或交易类型明细。用500字以内概括主要出入账及类别。出账与消费、历史余额与当前余额要区分，说明不能据此确定的事情。`));
  get('history-forecast').addEventListener('click', () => analyze('依据这份MoneyData流水，现在再花3500安全吗？请检查预测所需资料是否齐全，不要假设币种和今天余额。'));
  return {show};
}
