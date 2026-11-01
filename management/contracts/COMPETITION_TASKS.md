# 持久业务任务合同 v1

验收：无环依赖按结果引用执行；准备操作必须暂停；仅银行真实回执推进成功；并发/崩溃/重启不重复执行成功动作；拒绝阻止依赖；重规划保留已发生事实。

`from bank_tasks import TaskService, TaskError`；构造 `TaskService(path, *, clock, tool_specs, call_tool)`。clock返回有时区datetime；tool_specs为银行工具spec列表或无参数函数；call_tool(name,args)返回现有 `{data:...}` 信封。任务数据库须与银行数据库分开，固定scope=bank/U1；不得把规划/确认/验证/任意代码工具加入此执行器。

- `submit(prompt, plan, idempotency_key)`：plan=`{"title":"...","nodes":[{"id":"accounts","tool":"accounts","args":{},"depends_on":[]}]}`。1—20节点，id以英文字母开头、后续字母数字下划线且总长<=40，title<=200，直接TaskService的prompt<=8000；参赛模型工具submit_task_plan的prompt上限为6000。同键同请求返回同任务，异参拒绝。
- 参数可用完整对象 `{"$ref":"node_id.data.operation.operation_id"}` 引用明确祖先节点的结果。禁止模板字符串/代码/动态工具名。普通参数在提交时校验，引用解析后按完整工具schema再次校验。prepare工具幂等键由引擎生成；模型可省略或给值，实际执行均由稳定内部键替换。
- `get(task_id)` 返回任务含nodes/results/audit；`list(limit=50, active_only=False)` 返回任务列表。任务包含task_id/prompt/title/scope/subject_id/plan_version/state/created_at/updated_at/nodes/plan_history。
- `tick(task_id)` 每次最多执行一个就绪节点，或核对一个待确认节点；返回最新任务。SQLite BEGIN IMMEDIATE覆盖一次本地工具调用，保证单消费者；禁止在call_tool内执行长模型调用或递归进入同一任务库。工具调用异常保存FAILED，不自动无界重试。
- 可信UI接口 `pause(task_id)`、`resume(task_id)`、`cancel(task_id)`、`retry(task_id,node_id)`、`handoff(task_id,reason)`；不放入模型工具目录。取消不会撤回已完成交易。存在仍有效PENDING银行操作时取消拒绝，UI应先对真实operation执行reject，再取消。暂停/接管不撤销已出示的银行确认，UI必须明确状态。
- `replan(task_id,expected_version,nodes,reason)`：乐观版本锁；原SUCCEEDED节点须原样保留(工具/参数/依赖)，不得重跑；待银行确认节点不得改动/删除，须先可信拒绝后tick核对。新版本修改未执行/失败节点产生新幂等键；不擦除旧计划与结果，保存在plan_history。重规划后任务READY，已成功部分保留。

节点状态：PENDING/RUNNING/WAITING_CONFIRMATION/WAITING_VERIFICATION/SUCCEEDED/FAILED/CANCELLED。任务状态READY/WAITING_CONFIRMATION/WAITING_VERIFICATION/FAILED/SUCCEEDED/PAUSED/CANCELLED/NEEDS_HUMAN。独立分支串行推进；失败时整任务暂停，先修复/重规划，不自动绕过拒绝。

银行准备结果PENDING→WAITING_CONFIRMATION，required_verification=STRONG→WAITING_VERIFICATION；SUCCEEDED须有真实receipt才成功；REJECTED/EXPIRED→FAILED。`operation`工具读取既有PENDING状态本身是成功只读查询，不等于执行业务。

崩溃边界：任务库事务回滚；prepare_*使用同任务/节点/计划版本稳定键在独立银行事务幂等恢复；只读可重读。执行器不提供confirm，任何真实记账发生在可信银行授权入口。新增有副作用工具必须先明确其幂等恢复保证；本合同仅允许固定白名单；check_insights会更新独立洞察库，prepare类会创建待确认操作，不能把整个执行器称为只读。任务失败返回错误码，不把异常原文/秘密回传模型。

调用意图先独立持久化至 `<任务数据库文件名>.intents.sqlite3`，备份/恢复时两库一起保存。该库不存账号、凭据或工具结果，只有稳定执行键和时点。即使银行已准备而任务事务因崩溃回滚，重规划/取消仍要求原键恢复核对，不能通过换版本绕开未知操作。Python SQLite连接均显式关闭，Windows重启/备份不依赖垃圾回收。每节点最多2次人工恢复，计划最多10版本；不自动重试。回调必须是本地有界确定性服务，不能直接接远程长请求；SQLite锁超时15秒。

当前固定节点白名单为 `accounts / transactions / spending_summary / arrangements / forecast / operation / event_context / prepare_transfer / prepare_mandate_query / prepare_mandate_cancel / match_mandate_from_query / propose_arrangement / billing_report / recurring_candidates / insight_status / check_insights / money_calculate`，且必须同时存在于可信注入的spec。prepare类包括propose_arrangement；不是所有节点都无持久化副作用。普通新增名称即使出现在spec中也不会自动授权，固定scope不能由plan或args改变。

money_calculate支持整数分加减、绝对差及left/right×100比例，零分母拒绝；返回计算结果不等于已验证输入的金融事实。可选来源ID只是声明，不会因填写ID就读取银行证据。参赛组合层提供该工具，不扩展历史MoneyData目录。

确定性拒绝补充：银行`bank_v1.common.DomainError`（兼容src命名入口）且code为已审查的准备前置失败（余额不足、账号/权限/参数/版本、已撤销或已关联）时，银行`connection`已回滚、未创建operation，清除对应未决意图并记录BANK_REJECTED_BEFORE_OPERATION，可取消或改参数重规划。仅有同名code的其他异常、超时、幂等冲突、未知结果不属于此保证。可信适配器不得在已提交准备后自行伪造此类银行前置异常；如果改为远程银行，须重新提供明确的未提交证明或按幂等键查询，不沿用此本地异常类型保证。

返回的task.state代表编排状态；nodes中的result保留工具信封。RUNNING是事务内瞬时状态，外部通常看到READY/等待/成功/失败；网页可据就绪节点展示下一步，不应谎称长模型思考发生在此执行器中。任务get包含audit；list只返回任务文档、不附完整audit，active_only仅READY及两种等待状态。

## 当前工具接入与重规划快照（兼容补充）

参赛组合层向模型提供 submit_task_plan、task_status、replan_task；前者顶层 idempotency_key 必须提供，节点prepare参数里的键才由执行器替换。task_status调用get只读当前任务，不执行tick；推进由后台任务泵负责。用户pause/resume/cancel/retry/handoff仍走可信入口，不加入模型目录。

get/task_status返回replan_guidance快照，包含当前版本、成功节点保留清单、变更节点需核实的操作及未知intent。CONDITIONAL只说明状态/版本未禁止提交，不是授权或执行保证；SUCCEEDED/CANCELLED及版本上限阻止重规划。FAILED可修复，PAUSED/NEEDS_HUMAN当前不自动推进但不禁止按条件重规划；成功重规划后按节点状态恢复推进。未成功节点有实际操作或未知提交时仍由replan实时守卫核实，不能用指导文字绕过原回执或确认。

## 非首项正式授权匹配（兼容补充）

match_mandate_from_query参数为query_operation_id、merchant_text、match_mode（normalized_equals或normalized_contains）。只接受本人已确认成功、同业务日且30分钟内的正式查询回执；零匹配、多匹配、已撤销、内容或版本变化拒绝，不选第一条。输出data.match.selected.mandate_id/version及data.match.query_operation_id，供后续引用；商户条件不是正则或授权指令。

包含匹配节点的新计划，其prepare_mandate_cancel节点必须依赖对应匹配祖先，mandate_id、expected_version、query_operation_id三参数引用同一匹配节点的对应字段。查询和撤销分别等待本人确认，匹配不代确认；旧无matcher计划与直接接口保留兼容边界。银行撤销不证明商户合同解除。

只有所有节点SUCCEEDED才能称任务全部完成；预测或查询节点成功、准备到PENDING、银行已成功但后续核对失败必须分开报告。只读结果保留不等于实时余额重查；新的准备仍受银行当时余额校验。

下一步我该怎么验证：运行 `python -m unittest discover -s tests/competition_tasks -v`；集成方另外运行真实银行确认、强验证、重启及DSH动态规划，不把模块测试当整体验收。
# 2026-10-12 验收后补充：引用格式

结果引用必须是 JSON 对象，例如 `{"$ref":"pay.data.operation.operation_id"}`。
如果把整个对象放入字符串（如 `"{$ref:\"pay.data.operation.operation_id\"}"`），
submit/replan 返回 `INVALID_REFERENCE`，说明正确格式；提交前即拒绝，既不创建任务，也不执行上游准备操作。
replan 校验失败保持原任务版本、节点结果和操作记录不变。有效引用、公开方法签名和恢复规则保持兼容。

