"""Current-turn, read-only preview field references. No intent or payment inference."""
from copy import deepcopy
import re
from src.bank_goals.evidence import digest
from .write_prohibitions import mask as mask_write_prohibitions

FORMAT = 'goal_profile_field_v1'
FIELDS = {'goal_title': 'title', 'purpose': 'user_reason', 'priority': 'priority_reason'}
CLAUSE = re.compile(r'[^，,。！？!?；;\n]+')
# Reject explicit uncertainty only; never parse connecting words into accepted identities.
NEGATION = re.compile(r'不是|并非|而非|不要|不想|不考虑|不打算|不买|不报|别买|别报|取消|放弃')
MULTIPLE = re.compile(r'或|还是|以及|分别|同时|另一个|另一项|两个目标|两项目标|、')


def require(value, code):
    if not value:
        raise ValueError(code)


def unresolved_source(message, values):
    """Conservative literal ambiguity boundary, not a general language parser."""
    scoped_message = mask_write_prohibitions(message)
    clauses = list(CLAUSE.finditer(scoped_message))
    locations = {}
    for field, text in values.items():
        require(message.count(text) == 1, 'UNRESOLVED_GOAL_PROFILE_SOURCE')
        start = message.index(text); end = start + len(text)
        clause = next((m for m in clauses if m.start() <= start and end <= m.end()), None)
        require(clause is not None, 'UNRESOLVED_GOAL_PROFILE_SOURCE')
        checked = clause.group()
        if field == 'priority':
            # A literal user preference may itself be negative (do not affect
            # repayment). Negation outside the quoted preference still rejects.
            local_start, local_end = start - clause.start(), end - clause.start()
            checked = checked[:local_start] + ' ' * len(text) + checked[local_end:]
        require(not NEGATION.search(checked), 'UNRESOLVED_GOAL_PROFILE_SOURCE')
        locations[field] = (start, end, clause)
    title, purpose = locations['goal_title'], locations['purpose']
    require(title[1] <= purpose[0] or purpose[1] <= title[0], 'UNRESOLVED_GOAL_PROFILE_SOURCE')
    first, second = sorted((title, purpose), key=lambda v: v[0])
    identity_region = scoped_message[first[2].start():second[2].end()]
    between_fields = scoped_message[first[1]:second[0]]
    # Punctuation alone is not uncertainty. Conjunctions inside a complete purpose
    # (e.g. work and study) are not treated as two goals either.
    require(not NEGATION.search(identity_region) and not MULTIPLE.search(identity_region) and
            not re.search(r'和|也', between_fields), 'UNRESOLVED_GOAL_PROFILE_SOURCE')


def resolve(item, action, context, records):
    """Return normalized text slots and host-only binding, or None for old format."""
    slots = item['slots']
    referenced = {k for k, v in slots.items() if type(v) is dict and 'reference' in v}
    if not referenced:
        require('goal_title' not in slots, 'GOAL_TITLE_REQUIRES_REFERENCE')
        return None
    require(item['intent'] == 'goal_compare' and action['effective_kind'] == 'ANALYZE' and
            referenced == set(FIELDS) and not set(FIELDS).intersection(item['missing_fields'] + item['conflicts']),
            'INVALID_GOAL_PROFILE_REFERENCE_SCOPE')
    rows = [r for r in records if r['tool'] == 'preview_goal_options' and r['ok'] is True and r['status'] == 200]
    require(bool(rows), 'GOAL_PROFILE_REFERENCE_WITHOUT_TOOL')
    row = rows[-1]; body = row['response']; profile = body['data']['unconfirmed_profile']
    require(row['args'] == {'profile': profile} and body['source_id'] == digest(body['data']),
            'GOAL_PROFILE_REFERENCE_INVALID_EVIDENCE')
    message = next(m['text'] for m in context['messages'] if m['message_id'] == context['current_message_id'])
    normalized, values = {}, {}
    for field, key in FIELDS.items():
        slot = slots[field]
        require(set(slot) == {'reference', 'source'}, 'INVALID_GOAL_PROFILE_REFERENCE')
        ref, source = slot['reference'], slot['source']
        require(type(ref) is dict and set(ref) == {'format', 'call_id', 'source_id', 'field'} and
                ref == {'format': FORMAT, 'call_id': row['call_id'], 'source_id': body['source_id'], 'field': key},
                'GOAL_PROFILE_REFERENCE_NOT_LATEST_CALL')
        require(type(source) is dict and set(source) == {'message_id', 'quote'} and
                source['message_id'] == context['current_message_id'] and
                type(source['quote']) is str and source['quote'] == profile[key] and
                1 <= len(source['quote']) <= 300 and source['quote'] in message,
                'GOAL_PROFILE_REFERENCE_UNBOUND_FIELD')
        values[field] = profile[key]
        normalized[field] = {'value': profile[key], 'source': deepcopy(source)}
    unresolved_source(message, values)
    return normalized, {'kind': FORMAT, 'item_id': item['id'], 'request_id': context['request_id'],
                        'message_id': context['current_message_id'], 'call_id': row['call_id'],
                        'source_id': body['source_id'], 'values': values}


def verify_component(item, component):
    binding = item.get('goal_profile_binding')
    if not binding:
        return
    meta = component['metadata']
    require(item['intent'] == 'goal_compare' and item['action']['effective_kind'] == 'ANALYZE' and
            binding['item_id'] == item['id'] and meta['format'] == 'goal_preview_answer_v1' and
            binding['call_id'] == meta.get('call_id') and binding['source_id'] == meta['body']['source_id'],
            'GOAL_PROFILE_REFERENCE_COMPONENT_MISMATCH')
    profile = meta['body']['data']['unconfirmed_profile']
    require(all(profile[key] == binding['values'][field] == item['slots'][field]['value']
                for field, key in FIELDS.items()), 'GOAL_PROFILE_REFERENCE_FIELD_MISMATCH')


def display(item):
    values = item['goal_profile_binding']['values']
    return (f"目标：{values['goal_title']}\n用途：{values['purpose']}\n"
            f"优先偏好：{values['priority']}（仅记录你的偏好，不表示相关支出已支付或风险已消除。）")


def model_instruction():
    import json
    return ('新目标只读preview_goal_options成功后，goal_compare/ANALYZE最终回答的goal_title、purpose、priority必须一起使用'
            '同一次本轮最新成功调用的完整字段引用，不重新合成purpose文本，不填写value：'
            '{"reference":{"format":"goal_profile_field_v1","call_id":"本轮实际call_id","source_id":"本次source_id",'
            '"field":"对应profile字段"},"source":{"message_id":"current_message_id","quote":"该profile字段完整原文"}}。'
            '固定字段映射：' + json.dumps(FIELDS, ensure_ascii=False) + '。三个quote须逐字等于对应完整profile值且在当前原话各出现一次；'
            '名称与用途不能重叠；普通逗号或分句不是歧义。身份来源覆盖的片段有明确否定、备选/多目标或归属不唯一时须澄清，'
            '不拼接词片、不猜目标；不支持的表达不能声称已核验。金额/最低额/保留额/两日期仍用原value/source逐项绑定。'
            '此引用仅用于只读预览，不用于保存目标或准备操作；最终组件必须引用同一preview source_id。'
            '明确要草案时用goal_compare/PREPARE调用propose_goal，request_binding及最终items均不填goal_title或reference；'
            '用purpose的原value/source对应profile.title/user_reason，priority对应priority_reason，金额和两日期逐项保留。'
            '准备草案的action.source.quote覆盖本项完整请求，不能只引用“准备”；title/user_reason必须逐字取自该项原文，'
            '分别唯一出现且没有否定、备选或多目标歧义，不能在真实用途后扩写其它目标；优先事项完整保留。'
            'goal_title没有普通value/source形式；不要把只读引用格式套到准备草案。成功后交付实际goal_draft_v1，'
            '草案尚未确认，不能当已保存focus调用goal_context，也不能用预览回答冒充已生成草案。'
            '若previous_items中的最新同目标项含宿主goal_preview_origin，当前明确要草案可continuation_of引用该项，'
            '只填写当前PREPARE动作和本轮明确修改的金额/两日期/priority slots，不重填旧goal_title或reference。'
            'title/user_reason必须原样沿用该父项已核验身份；当前出现预算/最低/保留额/期望日期/最晚等修改，'
            '必须逐项声明当前slot，不得遗漏后沿旧值保存；未支持或歧义修改先澄清。旧预览金额预测不是当前银行事实。'
            'goal_profile_binding等内部标记禁止自行填写。旧格式和已保存目标查询仍按原合同。\n')
