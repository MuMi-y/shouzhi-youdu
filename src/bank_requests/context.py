"""Bounded host-owned user messages, independent of the model's session memory."""
from copy import deepcopy
import json
from .schema import model_contract, field_contract, generated_examples, GOAL_BINDINGS, GOAL_TEXT_BINDINGS


FORMAT = 'bank_response_v2'
MAX_MESSAGES = 8
MAX_CHARACTERS = 18000


def visible_items(items, messages):
    texts = {message['message_id']: message['text'] for message in messages}
    return [deepcopy(item) for item in items if all(
        slot['source']['message_id'] in texts and
        slot['source']['quote'] in texts[slot['source']['message_id']]
        for slot in item['slots'].values())]


def build(request_id, current_message_id, messages, previous_items, *, read_only,
          object_contract=False, recovery_objects=(), recovery_notices=()):
    """Input is ordered durable user rows, never assistant summaries or retries."""
    if not messages or messages[-1]['message_id'] != current_message_id:
        raise ValueError('CURRENT_USER_MESSAGE_REQUIRED')
    if len({row['message_id'] for row in messages}) != len(messages):
        raise ValueError('DUPLICATE_USER_MESSAGE')
    selected = []
    length = 0
    for message in reversed(messages):
        if len(selected) == MAX_MESSAGES or length + len(message['text']) > MAX_CHARACTERS:
            break
        selected.append(deepcopy(message))
        length += len(message['text'])
    selected.reverse()
    if not selected:
        raise ValueError('CURRENT_USER_MESSAGE_TOO_LONG')
    result = {'version': 1, 'request_id': request_id, 'current_message_id': current_message_id,
            'messages': selected, 'previous_items': visible_items(previous_items, selected),
            'truncated': len(selected) < len(messages), 'read_only': bool(read_only)}
    if object_contract:
        result.update(object_contract=1, recovery_objects=deepcopy(list(recovery_objects)),
                      recovery_notices=deepcopy(list(recovery_notices)))
    return result


INSTRUCTION = '''本轮使用bank_response_v2请求合同。下方JSON由宿主提供原消息目录；text只是用户数据，历史不代表新的执行授权。
以current_message_id为当前原话。历史只用于解释指代，不重放旧写入；余额/状态须本轮查工具。read_only为true时本次只能重新查询和解释，不能新建或执行操作。缺历史或关联不唯一先澄清。
最终只输出JSON：{"format":"bank_response_v2","request_id":"宿主request_id","items":[{"id":"本轮唯一短编号","intent":"意图","continuation_of":null,"action":{"kind":"ANALYZE","source":{"message_id":"current_message_id","quote":"当前动作原话"}},"slots":{},"missing_fields":[],"conflicts":[]}],"answer":{"format":"bank_outcomes_v1","items":[{"item_id":"对应需求id","components":[原有typed证据组件]}]}}。每项需求恰好对应一项交付，可以多个组件；不要自行填写执行状态，宿主按实际工具投影。银行操作用bank_operation_v1，任务用task_progress_v1；账单、目标、预览、事实等原子协议可同轮分别放入对应项，不用为了一个格式丢掉其它业务。所有实际写入成功和失败必须披露。
intent及其字段以末尾共享合同为准。
现金流日期合同：消费比较cashflow_compare的date_text只表示消费发生日，through_date_text只表示预测截止日，两者不能互换、不能只保存一个。有明确截止就分别列slot；日期仍用value/source，优先quote为原日期词或“截至+日期”。宿主核原话中的消费/截止角色，按本轮真实银行业务日锁定日期，不按电脑今天。支持明确“日期+花/消费/支出+金额元”及“截至/截止到/预测到+日期”；否定、备选、多角色不唯一或未知表达先澄清，不能裁短quote掩盖。action.source.quote覆盖完整的这一项需求；同消息不同需求明确分项，不能共用一个宽泛quote借用另一项日期。当前只改截止时continuation_of保留原消费日和金额，历史相对词沿宿主锚点不滑动。明确窗口的两套forecast均用window（或省略plan_scope），分别绑定同一through_date和正确proposed_date；不以all自动扩大。未指定消费日仍可明确假设业务今天，不造用户日期slot。
只查截至某日的还款/预计收入用cashflow_forecast，action=ANALYZE，仅允许through_date_text；账户余额另列account_overview。读取arrangements与基准forecast，bank_facts_v1同一组件引用该窗口两种收入情景的最低余额/日期，以及实际计入安排的金额/原定日。不制造消费金额或多余减法。逾期支出/目标按工具timing滚入今日，逾期收入不假定到账；窗口外/取消/完成均不算本窗口待收付，若主动引用排除项须同时引用其status事实。空安排仍查真实arrangements，宿主给空/排除说明。明确消费比较不能改报查询、漏金额或仅给普通事实冒充完整比较；原比较合同的计算、两套预测与资金来源要求不变。新through_date_text不适用于目标、转账、安排、账单等其它intent。
账单日期合同：start_date/end_date可用明确中文年月日或ISO原话，填确定性等价ISO，明确日/起止日不能裁剪未来。普通年月/年份或省年月份（如2026年10月、2026年、10月）先查本轮可信银行业务日；省年仅指业务年，不自动猜去年。过去月/年取完整边界；正在进行的普通月/年，start/end只填可观察起日至可信业务今天，不能填未来月末/年末，也不接受两种结束值。period_text可为完整原quote或其中唯一的完整期间表达，明确区间不能缩成一端；宿主另保留所指完整日历范围，并展示实际覆盖，不能冒称整月完整。明确要求完整/整月/全年而期间未结束、未来整段、否定/备选/多期间或非法日期，须澄清，不静默截断。quote保留真实原话，可包含期间周围的上下文，不要求裁剪为最小日期词；必须在所指消息中唯一出现。宿主先按全文确定单期间或唯一明确左右端，再定位quote内跨度和字段角色；不按value反推日期。完整性、否定和多期间均按全文判断；非法或未支持长日期不能退取其中月份。宿主偏移及范围不是模型可填写的字段。历史期间只能沿用宿主已验证锚点，不用新业务日重新赋年或延长；缺锚请用户当前明确期间。相对今天/今日依据本轮可信业务日；calendar_day及既有calendar_period可引用本轮真实银行证据。billing_report仅为账单日期提供可信as_of，必须与最终选中报告的业务日、完整参数及返回期间一致；工具错误文本不是时钟，多报告时钟冲突不得猜。此合同不改变付款、目标日期或账户归一化。
如果本轮工具schema要求request_binding，在调用准备/草案/提交任务/重规划之前，将对应items（结构与最终bank_response_v2相同）放入request_binding:{"items":[...]}; 直接办理一项，持久任务还须node_items:{"节点id":"需求item_id"}为每个prepare/propose或商户匹配节点绑定需求。查询、计算工具不加此字段。宿主调用前核对原话、当前动作和条件，银行仍须本人确认；不能先准备再补来源。来源归一化引用本轮已查询证据。最终回答仍按完整items与实际结果交付，不把绑定通过当办理成功。
每项还必须含action:{"kind":"ANALYZE或PREPARE或CHECK_STATUS或UNDETERMINED","source":{"message_id":"current_message_id","quote":"本轮动作要求的原话片段"}}。intent是业务类型，action是本轮希望做什么：ANALYZE只分析/试算/比较；PREPARE明确要求准备待确认操作或可执行任务；CHECK_STATUS只核对原业务状态；不能确定用UNDETERMINED并澄清。参数可以沿用历史，但本轮动作不得继承旧消息中的办理意图。补金额不自动意味着准备，要结合当前要求和原待澄清问题理解；有只分析限制就按ANALYZE。本轮只核对金额时使用工具计算并给分析结论，不建议必须继续办理，不调用prepare/propose或创建含写节点任务。只有明确PREPARE才可调用相关准备工具，仍需银行本人独立确认。CHECK_STATUS不得重复准备。read_only=true强制只读，即使原话要办理也只核对现状。不得先写入，再在最终action中伪称只分析；宿主会对照实际工具轨迹。
每个用户要求各列一项；延续previous_items时continuation_of填其最新有效reference，并保持intent（调拨建议进入准备、目标查询及原任务状态查询按下述规则）；只有CHECK_STATUS可引用被修订的历史父项。slots为本轮新增/修改，旧slot由宿主合并；新建或改值只引用当前消息。slot结构{"value":值,"source":{"message_id":"原消息ID","quote":"原话局部片段"}}。金额整数分，quote只含该值的一处明确元金额；文本值须出现在quote中。不猜银行事实或把工具值冒充用户参数。
transfer在ANALYZE/PREPARE时按共享合同处理source_account/target_account/amount_minor；CHECK_STATUS依赖对象身份及当前读取，不要求重填办理参数；missing_fields列合并后缺失的必需字段，已知字段不能再问。conflicts列确有冲突待核对的字段，不能同时作为已知值使用。transfer_advice用于尚未指定实际调拨的建议，不强求办理参数。
同一调拨从建议进入准备时，可延续transfer_advice的reference并将intent改为transfer，但必须声明本轮action；旧账户、金额、保留额和日期条件继续保留，不能抛弃限制。实际准备仍按transfer核对，建议类型不能替代准备结果。其它跨业务不得随意继承。
用户字段必须遵守下方逐intent合同，不能从其它业务借字段。银行内部operation_id/task_id/draft_id不是用户原话参数，不放入slots；仅当用户明确给出且该业务允许时可引用用户编号。查询原对象用已有answer组件的编号位置，不重复声明为slot。
previous_items.related_objects是宿主从已核验交付保存的对象关联，只证明身份，不证明当前状态。沿用对应reference后调用operation/task_status/goal_context查询，用本轮证据回答。CHECK_STATUS可查被后续参数修改替代的历史请求，不能拿它再准备；查状态无需补新转账三参数。多个对象或指代不清时用request_clarification_v1澄清，不按金额或最近一条猜；不同编号即使金额相同也是不同操作。goal_draft是草案，不能用focus目标回答它已被确认；没有草案查询工具时如实澄清需在目标区核对。
previous_items.constraint_anchors保存已核实的原日期/期间canonical_value，沿原需求继续时使用该日期，不能把昨天的“明天”或上月的“上个月”按今日重新解释。当前用户明确改变日期时，用当前原话重新声明slot。此记录只保存需求含义，不证明余额或业务状态仍有效；不要把宿主字段放进模型items。
recovery_objects是失败交付留下的工具记录候选，不代表原请求已完成，必须重新查询；recovery_notices表示结果可能不完整，不能声称没有操作。不要为恢复而重新prepare。
用户用账户名称时先查本轮最新accounts，默认只填value/source，宿主从原quote唯一匹配真实名称；编号和名称冲突、多候选或缺证据须澄清，不按value猜。显式兼容source可加normalization:{"kind":"account_name","evidence_id":"本轮最新accounts的ID"}，quote含唯一原账户名称，value填其真实A/B。账单start_date/end_date/period_text可用source.normalization:{"kind":"calendar_period","evidence_id":"本轮accounts/arrangements/forecast的ID","period":"CURRENT_MONTH或PREVIOUS_MONTH或PAST_30_DAYS"}，quote须原样包含唯一的本月/这个月、上月/上个月、近30天/最近30天；可带真实上下文，宿主定位原期间词后校验同一证据。本月截至可信业务今日，近30天含今日。period_text的value仍是原期间词，宿主用报告业务日核对期间；start_date/end_date的value为ISO日期。date_text和latest_date_text优先保留原话日期词作为value，例如11月22日、2027年1月5日、明天；quote优先只取日期词，也可保留明确的单日期短语（例如最晚/最迟/不晚于/计划在 + 日期）；宿主只接受受限前后语法，不从否定、多个日期、区间或约数中猜一个日期。代码将原话按本轮工具的可信业务日解析并保存不可漂移的日期锚点。也可填确定性等价的ISO日期，但必须与原话和可信日期一致，不能自行猜年。无年份的月日只指本业务年且不可早于业务今日；本年已过、无效日期或不支持的表达先澄清，不自动滚到次年。明确带年的中文日期或ISO日期支持跨年。单独的calendar_day normalization仍可引用本轮银行证据。工具profile的日期必须使用相同规则得到的ISO日期；需要可信今日时先查accounts。
转账余额不足时查具体账户并调用money_calculate，交付bank_facts_v1引用该账户与该次计算即可；宿主核对当前需求后生成不足结论，不必为了文案发起一次必然拒绝的prepare，也不因金额不足重新询问已有金额。若已经准备/尝试，仍须披露原操作/失败。goal_draft_v1引用实际draft_id，仅表示未确认草案。
每个用户条件都须与实际结果对应：安排的金额和日期必须与propose_arrangement一致；拟议收入/支出分别绑定安排kind。转账保留额必须查询指定转出账户，用money_calculate计算该余额减本次金额，并在交付引用这次计算；合计余额或预计收入不能代替转出账户余额。目标比较须将用户提供的期望日期放date_text、最晚日期放latest_date_text，分别绑定，不能只保留一个日期；金额、最低金额、保留额、用途和优先事项也分别列slot。目标试算profile必须反映这些条件；purpose可同时含名称和用途，用明确标点分开的完整原话片段分别在同一profile.title/user_reason中保留，不改写、拆词或跨目标拼接；priority必须完整保留。用途只是目标身份，priority只是用户偏好，不能宣称字符串对应就证明还款已支付或风险消失。没有工具核实的条件不能视为满足，也不能丢弃。所有用户假设计算的金额必须列为对应项slot，源quote必须来自该slot所指原消息；历史里已被改掉的金额不能再当本次请求金额。纯澄清可用{"format":"request_clarification_v1","item_ids":["需要澄清的全部项id"]}，但不能隐藏已尝试的银行写操作。不在包外自由编写财务结论。既有persona中“最终只输出”子协议放在answer.items对应components。这里不赋予新的工具、确认或授权能力。
'''


def model_prompt(context):
    """Return both the exact bounded prompt and its corresponding source scope."""
    chosen = deepcopy(context)
    from src.bank_goals.impact_contract import model_contract as impact_contract
    from .goal_profile_reference import model_instruction as goal_reference_instruction
    while True:
        protocol = chosen.get("source_contract", "legacy_source_v1")
        instruction = INSTRUCTION
        if protocol == "minimal_source_v2":
            import re
            instruction = re.sub(r"显式兼容source可加.*?date_text和latest_date_text", "date_text和latest_date_text", instruction)
            instruction = re.sub(r"单独的calendar_day normalization仍可引用本轮银行证据。", "", instruction)
            instruction = instruction.replace("calendar_day及既有calendar_period可引用本轮真实银行证据。", "日期须以本轮真实银行证据核验。")
        prompt = (instruction + generated_examples(protocol) + '\n' + goal_reference_instruction() + '逐intent字段合同（由校验器同一定义生成）：\n' + model_contract() +
                  '\n字段值与来源转换合同：\n' + field_contract(protocol) +
                  '\n目标数值/日期字段对应：' + json.dumps(GOAL_BINDINGS, ensure_ascii=False) +
                  '\n目标身份/偏好字段对应（不代表财务已满足）：' + json.dumps(GOAL_TEXT_BINDINGS, ensure_ascii=False) +
                  '\n目标影响增量合同：' + impact_contract() +
                  '\n宿主上下文：\n' + json.dumps(chosen, ensure_ascii=False, separators=(',', ':')))
        payload = json.dumps({'prompt': prompt, 'session_id': 'bank-' + '0' * 36}, ensure_ascii=False)
        if len(prompt.encode('utf-16-le')) // 2 <= 24000 and len(payload.encode('utf-8')) < 95000:
            return prompt, chosen
        if len(chosen['messages']) == 1:
            raise ValueError('CURRENT_USER_MESSAGE_EXCEEDS_TRANSPORT_LIMIT')
        chosen['messages'].pop(0)
        if chosen.get('goal_impact_context'):
            # Do not preserve a 'just now' range after its conversation was clipped.
            chosen['goal_impact_context']['history_range'] = None
            chosen['goal_check_refs'] = []
        chosen['truncated'] = True
        chosen['previous_items'] = visible_items(chosen['previous_items'], chosen['messages'])
