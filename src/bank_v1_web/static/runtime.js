/* Process capability and historical results are deliberately separate. */
export function renderRuntimeStatus(state, documentRoot) {
  const get = id => documentRoot.getElementById(id);
  const model = state.model || {};
  const pending = state.jobs.some(job => ['QUEUED', 'RUNNING'].includes(job.status));
  const unavailable = model.managed && !model.ready_for_requests;
  const heading = !model.managed ? '运行状态未登记' : model.mode === 'NO_MODEL' ? '工具模式 · AI未启动' :
    !model.worker_running ? 'AI工作进程未运行' : pending ? 'AI正在处理请求' :
    unavailable ? 'AI暂不可用' : 'AI已启动 · 可以提交需求';
  get('runtime-title').textContent = heading;
  get('runtime-summary').textContent = model.reason || '请使用产品标准入口查看准确运行状态。';
  get('runtime-summary').className = unavailable ? 'warning' : 'caption';
  get('agent-status').textContent = heading;
  const budget = model.budget;
  const until = budget?.deadline_ms ? new Date(budget.deadline_ms).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai'}) : '';
  const details = [`版本 ${model.build_id || '未登记'}；业务日期 ${state.business_date}（北京时间）。`,
    `配置路线 ${model.runtime || 'DSH'} / ${model.model || '未登记'}。状态查询不会调用模型，不代表供应商已验证成功。`];
  if (budget && budget.state !== 'NOT_USED') details.push(
    `本时段剩余AI运行 ${budget.dsh_remaining ?? '未知'} 次（含后台检查），模型请求 ${budget.provider_remaining ?? '未知'} 次。${until ? `截止 ${until}（北京时间）。` : ''}重启不会重置额度。`);
  if (unavailable) details.push('查看和确定性计算仍可用。停止产品后再次打开“启动有数”，可选择下一使用时段；已有记录不会清空。');
  get('runtime-details').textContent = details.join('\n');
  const recent = [...state.jobs].sort((a,b) => new Date(b.updated_at) - new Date(a.updated_at));
  const success = recent.find(job => job.status === 'DONE' && job.result?.ok);
  const failure = recent.find(job => ['FAILED', 'INTERRUPTED'].includes(job.status));
  const when = job => new Date(job.updated_at).toLocaleString('zh-CN', {timeZone:'Asia/Shanghai'});
  get('runtime-last').textContent = `${success ? `最近成功回答：${when(success)}` : '尚无成功回答记录'}；${failure ? `最近失败：${when(failure)}，原因见相应记录` : '尚无失败记录'}。历史成功不保证下一次成功。`;
  documentRoot.querySelectorAll('#send, [data-prompt], #history-analyze, #history-forecast').forEach(button => {
    button.disabled = unavailable || button.dataset.busy === 'true';
  });
}
