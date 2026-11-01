# 参赛版账单与持续关注合同

## 验收标准
完整期间账单可核算且来源可追溯；退款和内部转账不冒称消费；周期候选至少三条证据且不当合同；反馈严格版本校验并持久化；模型不能确认关注或办理代扣；提醒去重、更正生效；回放只读截至时点资料。

## 数据与边界
只用 Python 标准库。独立 insights SQLite 保存候选状态、关注项、提醒和反馈审计；银行连接以 URI mode=ro/query_only 打开并固定 U1，不访问 mandates。账单来自完整只读快照，不用 state 的500行截断。历史范围以 business_date 和创建时刻共同限定。金额整数分、显示字符串；银行CNY，MoneyData独立纯函数探索的currency=null。不推断正式合同、个人真实计划或欺诈结论。

## 稳定接口
`InsightService(db_path, *, bank, clock)`；clock 返回有时区 datetime。`tool_specs()`、`call_tool(name,args)`、`user_action(name,args)`、`state()`、`check()`。

工具：`billing_report{start_date,end_date,compare_previous?:bool}`，默认对照此前等长期间；`recurring_candidates{}`；`insight_status{}`；`check_insights{}`。前三者只读，本模块check只保存分析结果，永不修改银行账户、授权或交易。包装含 evidence_id、scope=bank、currency=CNY、as_of、data。

可信动作：`candidate_feedback{candidate_id,expected_version,decision:confirm|reject|ignore|correct,label?,amount_minor?,next_due_date?}`。除correct外不接受修订字段；correct至少一项。confirm只代表用户确认关注周期性支出，不是查询正式代扣或撤销授权的同意。旧版本不执行。重复相同版本反馈拒绝STALE_VERSION，由调用方重新读取。

候选字段：candidate_id、merchant_key、label、account_id、currency、period_days、evidence_refs、observed_dates、amount_minor、previous_amount_minor、next_due_date、algorithm_version、version、status、confirmed、corrections、evidence_hash。状态proposed/confirmed/rejected/ignored；correct保留关注并标user_corrected。观察证据变更增加版本；商户描述仅作不可信数据，不执行指令。关注更正字段优先，后续扫描不覆盖；拒绝和忽略持续有效。

提醒：RENEWAL_DUE（已确认，未来0–7天）、PRICE_INCREASE（最新连续扣费高于此前至少两次相同金额，增幅达到已生效的 recurring_price_increase 阈值，默认10%）、MISSED_EXPECTED_CHARGE（已确认预期日过7天仍无同商户新扣费，仅提示未观察到、不称违约）、候选默认为列表展示不主动轰炸。指纹绑定对象/类别/版本依据；重复检查无新提醒。纠正/忽略将旧提醒标superseded，过期的临近提醒同样失效。日期更正覆盖下一次观测；若新出现的同商户周期证据日期达到更正日（容差提前2天），将该次覆盖保存至correction_history，后续日期恢复据新证据推算，不冒称合同履约。金额/名称更正继续保留。资金风险继续由已有bank_v1事件模块负责，不能重复建设另一份预测。

纯函数 `build_report(rows,start_date,end_date,*,currency,compare_previous=True,policy=None)` 与 `detect_recurring(rows,*,as_of,currency,policy=None)` 接收规范化已知交易；各行id/date/account_id/kind/amount_minor/description/category/source。调用方保证身份和来源，纯函数仍按as_of/期间过滤。月周期允许25–35天，周周期6–8天；至少3笔且最近所有间隔符合一种周期，取最近6笔。仅expense/outflow且排除声明的transfer/refund/opening；同一天同额同商户仅有限异常候选不当两次周期证据。

## 已实现的规则配置与报告范围（兼容补充）

原构造和工具参数不变；可选 `policy` 省略时取默认规则。可信部署者在停机时配置运行目录根部的 `<DataDir>/insight-policy.json`，启动校验后持久化配置版本与指纹；模型和普通网页不能改规则。具体结构和修订规则见[运行说明](../../docs/COMPETITION_RUNBOOK.md)。工具 data 同时提供 analysis_policy 与 policy_config，阈值是项目规则，不是用户预算或独立业务事实。

账单报告增加 period_metadata、metrics、description_groups、description_comparison 和 policy_application；旧字段仍保留。请求日期按含首尾天数分类为完整单自然月或任意窗口，不证明资料完整，不将等长前期自动称上一个自然月，也不外推未来每月结余。changes 给出期间指标差额；描述×交易类型分组的差额和比例使用各组自己的计算结果，不能挪用碰巧相等的整期数字。描述分组展示有上限，隐藏组净差为零不证明其余组未变。

类别显著上涨规则只评估 category 汇总出账，使用正的前期基数、相对增幅及绝对增额两个阈值（默认20%及10000分）；policy_application 标明作用范围。它不适用于单条流水、原描述组或周期序列。当前银行适配器 category 来自交易 kind，并非模型推断的商户行业。周期序列另用 recurring_price_increase（默认10%），不把规则阈值当实际观察涨幅。

## 缓存、刷新与提醒分组（兼容补充）

recurring_candidates 和 insight_status 读取缓存；用户要求重新检查变化时调用 check_insights，它只更新独立洞察库。state 不扫描银行全量流水；后台任务泵在流水变化或日/周检查到期时刷新。账单查询仍读取完整已授权快照。

state 保留 notifications 最近100条历史，同时从全部 active 通知形成 notification_groups，不以历史上限截掉当前原因。分组键为 candidate_id、currency、due_date、amount_minor；同对象的一张卡保留续期/涨价等各自原因、通知ID及证据，不把两种原因算作两笔扣款。check 另返回 new_notifications/new_notification_groups，只有新建通知进入本次新增集合。

忽略后重新确认关注，若原通知仍满足当前条件，可恢复相同通知ID，不计为新推送；日期/金额变化或条件已经失效的旧通知不会因此复活。配置变更可以改变有效原因，不修改银行账本、正式授权或候选原流水证据。

## 下一步我该怎么验证
开发与独立验收运行tests/competition_insights及scripts/competition_insights_evaluate.py；检查全量账单、跨用户/未来过滤、反馈权限、版本冲突、重复/退款/静默及重启，再阅读合成真值与无标签公开历史分开的评估。用户无需运行技术测试。
