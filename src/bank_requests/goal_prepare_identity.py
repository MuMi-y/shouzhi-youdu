"""Host-only literal identity binding for unconfirmed goal drafts.

This deliberately bounded check is not a general intent parser. Ambiguous
identity clauses must be clarified, never repaired into a different profile.
"""
from copy import deepcopy
import re

from src.bank_v1.common import DomainError

CLAUSE = re.compile(r'[^，,。！？!?；;\n]+')
NEGATION = re.compile(r'不是|并非|而非|不要|不想|不考虑|不打算|不买|不报|别|取消|放弃|暂不|无需|不需要')
MULTIPLE = re.compile(r'或|还是|以及|分别|同时|另一个|另一项|两个目标|两项目标|、|和|与|及|也')
FORMAT = 'goal_prepare_identity_v1'
DENIED_PREPARATION = re.compile(r'(?:不要|不想|不打算|别|暂不|无需|不需要|取消)[^，,。！？!?；;\n]{0,6}(?:准备|保存|创建|生成|提交|办理)')
UNDECIDED = re.compile(r'或|还是|没选定|未选定|尚未选|没确定|未确定|未决定|没决定|另一个|另一项|两个目标|两项目标|分别')
PARALLEL = re.compile(r'和|与|及|也|同时|、')


def require(condition):
    if not condition:
        raise DomainError('REQUEST_GOAL_IDENTITY_UNBOUND',
                          '目标名称、用途或偏好未唯一对应完整原话，请明确单个目标后再准备草案；尚未写入。')


def source_scope(slot, item, context):
    source = slot['source']
    message = next(row['text'] for row in context['messages'] if row['message_id'] == source['message_id'])
    quote = source['quote']
    require(message.count(quote) == 1)
    start, end = 0, len(message)
    if source['message_id'] == context['current_message_id']:
        action_quote = item['action']['source']['quote']
        require(message.count(action_quote) == 1)
        start = message.index(action_quote)
        end = start + len(action_quote)
    quote_start = message.index(quote)
    require(start <= quote_start and quote_start + len(quote) <= end)
    return {'message_id': source['message_id'], 'message': message,
            'scope': [start, end], 'source': deepcopy(source), 'value': slot['value']}


def bind(item, context):
    """Called only after decision validation; the model cannot supply this field."""
    current = next(row['text'] for row in context['messages'] if row['message_id'] == context['current_message_id'])
    action_quote = item['action']['source']['quote']
    require(current.count(action_quote) == 1)
    action_start = current.index(action_quote)
    action_end = action_start + len(action_quote)
    require(not any(DENIED_PREPARATION.search(clause.group()) for clause in CLAUSE.finditer(current)
                    if clause.start() < action_end and action_start < clause.end()))
    purpose = item['slots'].get('purpose')
    require(purpose is not None)
    return {'format': FORMAT, 'purpose': source_scope(purpose, item, context),
            'priority': source_scope(item['slots']['priority'], item, context)
            if 'priority' in item['slots'] else None}


def locate(text, origin):
    message = origin['message']
    require(isinstance(text, str) and bool(text) and message.count(text) == 1)
    start = message.index(text)
    end = start + len(text)
    require(origin['scope'][0] <= start and end <= origin['scope'][1])
    clause = next((m for m in CLAUSE.finditer(message) if m.start() <= start and end <= m.end()), None)
    require(clause is not None)
    return start, end, clause


def check_request_scope(message, reason=None):
    """One complete source message owns a single goal, never a clipped clause.

    Ordinary conjunctions may be inside an exact, unique complete purpose.
    Alternatives/undecided choices are never exemptions, even in that purpose.
    """
    require(not UNDECIDED.search(message))
    allowed = None
    if reason and message.count(reason) == 1:
        start = message.index(reason)
        allowed = (start, start + len(reason))
    require(all(allowed and allowed[0] <= match.start() and match.end() <= allowed[1]
                for match in PARALLEL.finditer(message)))


def check(profile, item):
    origin = item.get('goal_prepare_identity')
    require(isinstance(origin, dict) and origin.get('format') == FORMAT)
    purpose = origin['purpose']
    parent = item.get('goal_prepare_parent')
    if parent:
        require(all(profile.get(key) == parent['identity'][key] for key in ('title', 'user_reason')))
    require(purpose['source'] == item['slots']['purpose']['source']
            and purpose['value'] == item['slots']['purpose']['value'])
    title = locate(profile.get('title'), purpose)
    reason = locate(profile.get('user_reason'), purpose)
    check_request_scope(purpose['message'], profile['user_reason'])
    first, last = sorted((title, reason), key=lambda value: value[0])
    # Inspect full original clauses, including words outside a clipped action.
    region = purpose['message'][min(title[2].start(), reason[2].start()):
                                max(title[2].end(), reason[2].end())]
    require(not NEGATION.search(region))
    overlap = first[1] > last[0]
    if overlap:
        require(title[2].span() == reason[2].span()
                and (title[0] <= reason[0] and reason[1] <= title[1]
                     or reason[0] <= title[0] and title[1] <= reason[1]))
    # The purpose quote itself must belong to this same identity region.
    purpose_start = purpose['message'].index(purpose['source']['quote'])
    require(min(title[2].start(), reason[2].start()) <= purpose_start
            and purpose_start + len(purpose['source']['quote']) <= max(title[2].end(), reason[2].end()))
    priority = origin['priority']
    if priority is not None:
        require(priority['source'] == item['slots']['priority']['source']
                and priority['value'] == item['slots']['priority']['value']
                and profile.get('priority_reason') == priority['value'])
        start, end, clause = locate(profile['priority_reason'], priority)
        # A preference may itself say '不要影响还款'. Only omitted negation
        # outside that exact preference is grounds to reject its attribution.
        surrounding = priority['message'][clause.start():start] + priority['message'][end:clause.end()]
        require(not NEGATION.search(surrounding) and not MULTIPLE.search(surrounding))
    else:
        # Even without a separate preference slot, do not invent profile text.
        locate(profile.get('priority_reason'), purpose)
