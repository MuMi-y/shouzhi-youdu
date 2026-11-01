"""One additive contract shared by tools, request coverage and rendering."""
import json
import re
from decimal import Decimal
from src.bank_v1.projection_journal import digest

FORMAT = 'goal_impact_v1'
CONTRACT = {
    'version': 1, 'format': FORMAT,
    'modes': {
        'WHAT_IF': {'tool': 'compare_goal_impact', 'stages': ['BEFORE', 'PROPOSED'],
                    'intents': ['cashflow_compare', 'goal_compare', 'goal_followup']},
        'POSTED_HISTORY': {'tool': 'explain_goal_changes', 'stages': ['BEFORE', 'AFTER_EXPENSE', 'AFTER_INCOME'],
                           'intents': ['goal_followup', 'cashflow_compare', 'goal_compare']}},
    'required': ['target', 'reserve_floor_minor', 'stages', 'income_conditions', 'coverage'],
    'answer': {'format': FORMAT, 'source_id': '本次工具source_id', 'mode': '与工具mode相同'},
    'instruction': '拟消费对已确认目标的影响用WHAT_IF；已入账消费及收入的连续影响用POSTED_HISTORY。'
        '不延续无关旧请求reference；用户换成新消费问题时新建item。两个工具只读，不能付款。'
        '目标身份用完整原名称加可选“计划/目标/安排”后缀，不把focus当名称。'
        'answer组件只引用source_id和mode；宿主必显全部阶段、保留底线和双情景。'
        '不要以普通forecast或goal_context替代关系分析。普通无目标试算及只问目标当前状态仍沿用旧协议。'
        '历史范围不唯一或缺起点时不得猜最后两笔；披露工具失败并请明确范围。'}


def model_contract():
    return json.dumps({'sha256': digest(CONTRACT), **CONTRACT}, ensure_ascii=False, separators=(',', ':'))


def specs():
    common = {'goal_id': {'const': 'focus'}, 'expected_revision': {'type': 'integer', 'minimum': 1}}
    return [{'name': cfg['tool'], 'description': CONTRACT['instruction'] + '模式：' + mode,
             'parameters': {'type': 'object', 'additionalProperties': False,
                 'properties': dict(common, **({'amount_minor': {'type': 'integer', 'minimum': 1},
                     'due_date': {'type': 'string', 'format': 'date'}} if mode == 'WHAT_IF' else {})),
                 'required': list(common) + (['amount_minor', 'due_date'] if mode == 'WHAT_IF' else [])}}
            for mode, cfg in CONTRACT['modes'].items()]


def names(target):
    title = (target.get('arrangement') or {}).get('title')
    return {target['profile']['title']} | ({title} if title else set())


def identity_matches(text, target):
    value = re.sub(r'[\s“”"「」]', '', text)
    return any(value == name + suffix for name in names(target) for suffix in ('', '计划', '目标', '安排'))


def requirement(text, target):
    """Limited explicit relation grammar, independent of model intent selection."""
    if not target:
        return None
    named = any(name in text for name in names(target))
    relation = bool(re.search(r'影响|底线|保留|缺口|变化|怎么变|恢复', text))
    posted = bool(re.search(r'消费|支出|花了', text) and re.search(r'入账|到账|收入', text)
                  and re.search(r'刚才|刚刚|这两|两笔|分别|先后|已经|变化|前后', text))
    if relation and posted and (named or re.search(r'目标|安排|保留|底线', text)):
        return 'POSTED_HISTORY'
    # Preparing/editing a goal is not itself a proposed expense. Exclude the
    # goal's noun phrase (which can contain 买/支出) from the consumption cue;
    # still detect a separate expense clause in the very same request.
    consumption_text=text
    for name in sorted(names(target),key=len,reverse=True):
        consumption_text=consumption_text.replace(name,'')
    if relation and named and re.search(r'消费|支出|花(?:掉|费)?|购买|购置|买', consumption_text):
        return 'WHAT_IF'
    return None


def what_if_inputs(text, business_day):
    """A small explicit consumption grammar; uncertain references remain unresolved."""
    from src.bank_requests.normalization import calendar_day
    amounts = list(re.finditer(r'(?:消费|支出|花(?:掉)?)\s*([0-9]+(?:\.[0-9]{1,2})?)\s*(万?元)', text))
    values = {int(Decimal(m[1]) * (1000000 if m[2] == '万元' else 100)) for m in amounts}
    dates = re.findall(r'\d{4}-\d{2}-\d{2}|(?:\d{4}年)?\d{1,2}月\d{1,2}[日号]|今天|明天|后天', text)
    days = {calendar_day(d, business_day) for d in dates}
    if not dates:
        days = {business_day}
    return {'amount_minor': next(iter(values)) if len(values) == 1 else None,
            'due_date': next(iter(days)) if len(days) == 1 else None,
            'amount_quotes': [m[0] for m in amounts], 'date_quotes': dates,
            'date_default': 'BUSINESS_TODAY' if not dates else None}


def enforce(required, components):
    if not required:
        return
    if any(c['metadata']['format'] == FORMAT and c['metadata']['body']['mode'] == required for c in components):
        return
    # An explicitly disclosed failure is a truthful incomplete outcome, never advice-ready.
    if any(c['metadata']['format'] == 'bank_tool_outcome_v1' and
           c['metadata']['attempt']['tool'] == CONTRACT['modes'][required]['tool'] for c in components):
        return
    raise ValueError('GOAL_IMPACT_RELATION_NOT_COVERED:' + required)
