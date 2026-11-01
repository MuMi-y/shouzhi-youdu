"""Limited evidence gate for bank answers, not a verifier of all natural language.

The gate rejects explicit unsupported action claims and unsupported displayed
amounts and percentages. It reads successful tool responses.
It never submits a banking action, confirms one, or changes an account.
"""
from decimal import Decimal, InvalidOperation
from datetime import date
import re
import json
from src.bank_tasks.validation import ALLOWED as TASK_NODE_TOOLS
from src.bank_v1_web.forecast_minimum import forecast_minimum_checks, iso, valid_minima
from src.bank_v1_web.billing_periods import billing_previous_period_checks
from src.bank_v1_web.billing_ratio_display import billing_ratio_display_checks


CONTRACT_VERSION = 'bank-evidence-34'
DECIMAL_AMOUNT = re.compile(r'(?<![A-Za-z0-9_.,])[-+−]?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}(?![\d.]|\s*[%％])')
CURRENCY_AMOUNT = re.compile(r'(?<![A-Za-z0-9_.,])[-+−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?=\s*元)')
PERCENT = re.compile(r'(?<![A-Za-z0-9_.,])[-+−]?\d+(?:\.\d+)?(?=\s*[%％])')
OP_ID = re.compile(r'\bop_[a-zA-Z0-9]+\b')
POSITIVE_PREPARE = re.compile(r'(?:已.{0,8}(?:准备|生成|备好)|(?:准备|生成|备好).{0,8}(?:就绪|完成|待.{0,3}确认))')
POSITIVE_EXECUTE = re.compile(r'(?:已取得(?:银行操作)?成功回执|已(?:读取|核对)(?:真实|银行)?回执|(?:已(?:经)?(?:执行成功|成功(?:执行|入账|划转|撤销|取消)?|入账|划转|撤销|取消)|(?:代扣|扣款)授权(?:已|被|成功|正式|经|予以){1,6}(?:取消|撤销)|(?:商户服务|会员服务|会员合同|商户合同)(?:已|被|成功|正式|经){1,6}(?:取消|撤销|终止)|(?:转账|查询|撤销|取消|操作)(?:已)?(?:执行)?(?:成功|完成|办妥)))')
WAIT_CONFIRM = re.compile(r'(?:请.{0,14}(?:确认|核对)|等(?:待)?.{0,8}确认|待.{0,3}确认)')
TASK_COMPLETE = re.compile(r'(?:任务.{0,10}(?:全部完成|全(?:部)?成功|已收尾)|(?:全部|所有).{0,5}节点.{0,6}(?:成功|完成))')
NON_ASSERTION = re.compile(r'(?:尚未|未曾|还没|没有|不能|无法|未能|并未|不会|不要|不代表|不等于|若|如果|如需|确认后|成功后|才会|才能|将会|我会|我再|想办理时|请告诉我)')
TASK_SAVED = re.compile(r'(?:任务已(?:经)?(?:建立|保存|创建|提交)|已(?:经)?(?:建立|保存|创建|提交).{0,4}任务)')


def operation_references(text):
    """Parse explicit display IDs, retaining malformed ellipses as references."""
    expression = re.compile(
        r'(?<![A-Za-z0-9_])(?P<prefix>op_[A-Za-z0-9]+)'
        r'(?:(?P<marker>(?:…|\.\.)[….]*)(?P<suffix>[A-Za-z0-9]*)'
        r'(?P<invalid_tail>_[A-Za-z0-9_.…]*|(?:…|\.\.)[A-Za-z0-9_.…]*)?)?')
    return [{'raw': match[0], 'start': match.start(), 'end': match.end(),
             'prefix': match['prefix'], 'marker': match['marker'] or '',
             'suffix': match['suffix'] or '', 'invalid_tail': match['invalid_tail'] or ''}
            for match in expression.finditer(text)]


def resolve_operation_references(text, operations):
    """Resolve against the whole visible trace before kind/state/target filters.

    All-full-ID claims keep their existing filter semantics. In a claim with
    an abbreviation, every explicit reference must exist in trusted evidence.
    """
    ids, unresolved = set(), False
    references = operation_references(text)
    has_abbreviation = any(reference['marker'] for reference in references)
    for reference in references:
        prefix, marker, suffix = (reference[key] for key in ('prefix', 'marker', 'suffix'))
        if not marker:
            ids.add(prefix)
            if has_abbreviation and prefix not in operations:
                unresolved = True
            continue
        matches = []
        if marker in {'…', '...'} and len(prefix) >= 11 and not reference['invalid_tail']:
            matches = [identifier for identifier in operations
                       if identifier.startswith(prefix) and identifier.endswith(suffix)
                       and len(identifier) > len(prefix) + len(suffix)]
        if len(matches) == 1:
            ids.add(matches[0])
        else:
            # Keep a visible reference for action-context detection as well as
            # the explicit failure flag; never degrade to an unqualified claim.
            ids.add(reference['raw'])
            unresolved = True
    return ids, unresolved


def visible_reference_operations(records):
    """Only result paths can supply abbreviated identities, never echoed inputs.

    This is deliberately separate from the legacy full-ID entity collector.
    Callers provide successful bank responses from this answer's current trace.
    """
    operations = {}

    def add(data):
        operation = data.get('operation') if isinstance(data, dict) else None
        if not isinstance(operation, dict):
            return
        identifier = operation.get('operation_id')
        if (isinstance(identifier, str) and OP_ID.fullmatch(identifier)
                and operation.get('kind') in {'transfer', 'mandate_query', 'mandate_cancel', 'arrangement'}
                and operation.get('status')):
            operations[identifier] = operation

    for record in records:
        data = record['response'].get('data', {})
        add(data)
        if record.get('tool') not in {'task_status', 'submit_task_plan', 'replan_task'} or not isinstance(data, dict):
            continue
        task = data.get('task')
        if (not isinstance(task, dict) or task.get('scope') != 'bank' or task.get('subject_id') != 'U1'
                or not task.get('task_id') or not isinstance(task.get('nodes'), list)):
            continue
        for node in task['nodes']:
            if (not isinstance(node, dict) or node.get('state') not in {
                    'SUCCEEDED', 'WAITING_CONFIRMATION', 'WAITING_VERIFICATION'}
                    or not isinstance(node.get('tool'), str)
                    or node.get('tool') not in {'operation', 'prepare_transfer', 'prepare_mandate_query',
                                              'prepare_mandate_cancel', 'propose_arrangement'}):
                continue
            result = node.get('result')
            if isinstance(result, dict) and result.get('scope', 'bank') == 'bank':
                add(result.get('data', {}))
    return operations


def claim_operation_entities(text, operations, reference_operations):
    """Use the actual trusted entity, not only its ID, for shortened references."""
    selected = dict(operations)
    if any(reference['marker'] for reference in operation_references(text)):
        ids, unresolved = resolve_operation_references(text, reference_operations)
        if not unresolved:
            for identifier in ids:
                if identifier in selected:
                    # Keep the caller's target scope, replace only the source
                    # entity used for an explicitly abbreviated reference.
                    selected[identifier] = reference_operations[identifier]
    return selected


def number(text):
    return Decimal(text.replace(',', '').replace('−', '-'))


# These expressions consume only explicit RMB labels, not arbitrary alphabetic
# prefixes. Legacy expressions below retain their identifier boundaries.
RMB_LABEL = re.compile(r'(?<![A-Za-z0-9_])(?:CNY|RMB|人民币)')
RMB_NUMBER = re.compile(r'[-+−]?(?:[0-9]{1,3}(?:,[0-9]{3})+|[0-9]+)(?:\.[0-9]{1,2})?')
RMB_RAW = re.compile(r'[A-Za-z0-9_.,/+:：\-−¥￥$€£%％]+')


def rmb_mentions(text):
    """Classify a full labelled token before any legacy partial-number scan."""
    mentions = []
    for label in RMB_LABEL.finditer(text):
        cursor = label.end()
        while cursor < len(text) and text[cursor] in ' \t': cursor += 1
        symbol = text[cursor] if cursor < len(text) and text[cursor] in '¥￥$€£' else ''
        if symbol:
            cursor += 1
            while cursor < len(text) and text[cursor] in ' \t': cursor += 1
        raw = RMB_RAW.match(text, cursor)
        if not raw:
            if not symbol: continue
            end, token = cursor, ''
        else:
            end, token = raw.end(), raw.group()
            # A label followed by ordinary prose is not an amount claim.
            if not symbol and token[0].isalpha(): continue
            if token in {'.', ',', '/', ':', '：'}: continue
        start = label.start()
        preceding_sign = re.search(r'[-+−][ \t]*$', text[:start])
        if preceding_sign: start = preceding_sign.start()
        broken_spacing = False
        if token and token[-1] in ',.' and len(token) > 1 and token[-2].isdigit():
            next_number = re.match(r'[ \t]+[0-9][A-Za-z0-9_.,/+:：\-−]*', text[end:])
            if next_number:
                end += next_number.end(); token = text[cursor:end]; broken_spacing = True
            else:
                # A single terminal comma/period can delimit a sentence/list.
                token, end = token[:-1], end-1
        foreign_suffix = re.match(r'(?:美元|欧元|日元|港元|港币)', text[end:])
        if foreign_suffix: end += foreign_suffix.end()
        status, match = 'INVALID_FORMAT', None
        if not preceding_sign and not symbol and not foreign_suffix:
            if '_' in token and re.fullmatch(r'[A-Za-z0-9_.]+', token):
                status = 'NOT_AMOUNT'
            elif re.fullmatch(r'[0-9]{4}[-/][0-9]{2}[-/][0-9]{2}', token):
                try:
                    date.fromisoformat(token.replace('/', '-')); status = 'NOT_AMOUNT'
                except ValueError:
                    pass
        if (status != 'NOT_AMOUNT' and not preceding_sign and symbol in {'', '¥', '￥'}
                and not broken_spacing and not foreign_suffix and RMB_NUMBER.fullmatch(token)):
            match = RMB_NUMBER.match(text, cursor)
            status = 'VALID'
        mentions.append({'origin': 'RMB_PREFIX', 'currency': 'CNY', 'status': status,
                         'span': (start, end), 'prefix_span': (label.start(), cursor),
                         'match': match, 'raw': text[start:end]})
    return mentions


def amount_mentions(text):
    """One occurrence, one decision; even invalid prefix tokens own their span."""
    mentions = rmb_mentions(text)
    occupied = [item['span'] for item in mentions]
    seen = set()
    for expression in (DECIMAL_AMOUNT, CURRENCY_AMOUNT):
        for match in expression.finditer(text):
            span = match.span()
            if span in seen or any(a < span[1] and span[0] < b for a, b in occupied): continue
            seen.add(span)
            mentions.append({'origin': 'LEGACY', 'status': 'VALID', 'span': span,
                             'match': match, 'raw': match.group()})
    return sorted(mentions, key=lambda item: item['span'])


def currency_semantic_text(text, mention):
    """Mask only this label with equal-length spaces; preserve numeric offsets."""
    if mention['origin'] != 'RMB_PREFIX': return text
    start, end = mention['prefix_span']
    return text[:start] + ' ' * (end-start) + text[end:]


def forecast_currency_scopes(tool, data):
    """Only known forecast result locations may use a non-permission scope."""
    path = () if tool == 'forecast' else ('forecast',)
    if tool not in {'forecast', 'event_context'} or not isinstance(data, dict): return set()
    projection = data if not path else data.get('forecast')
    if not isinstance(projection, dict) or projection.get('scope') != 'COMBINED_ACCOUNTS': return set()
    start, end = projection.get('as_of_date'), projection.get('through_date')
    daily = projection.get('daily')
    if (not iso(start) or not iso(end) or start > end
            or type(projection.get('current_balance_minor')) is not int or not isinstance(daily, list)):
        return set()
    fields = ('expected_income_minor', 'planned_outflow_minor', 'proposed_expense_minor',
              'projected_balance_minor', 'without_future_income_minor')
    if any(not isinstance(day, dict) or not iso(day.get('date')) or not start <= day['date'] <= end
           or any(type(day.get(key)) is not int for key in fields) for day in daily): return set()
    paths = {path}
    if valid_minima(projection.get('scenario_minima'), projection):
        paths.add(path + ('scenario_minima',))
    return paths


def currency_clean_data(value, domain_scopes=frozenset(), path=()):
    """Conflicting child currency or permission scope never inherits CNY bank."""
    if isinstance(value, list):
        return [currency_clean_data(item, domain_scopes, path + (index,)) for index, item in enumerate(value)]
    if not isinstance(value, dict): return value
    if ('currency' in value and value['currency'] != 'CNY'
            or 'amount_unit' in value and value['amount_unit'] != 'minor'): return {}
    if 'scope' in value and value['scope'] != 'bank':
        if value['scope'] != 'COMBINED_ACCOUNTS' or path not in domain_scopes: return {}
    excluded = {'args', 'arguments', 'inputs', 'plan_history', 'task', 'tasks'}
    return {key: currency_clean_data(item, domain_scopes, path + (key,))
            for key, item in value.items() if key not in excluded}


def pending_cny_node(node):
    """A successfully prepared operation is evidence of its amount, not success."""
    kinds = {'prepare_transfer': 'transfer', 'prepare_mandate_query': 'mandate_query',
             'prepare_mandate_cancel': 'mandate_cancel', 'propose_arrangement': 'arrangement'}
    result = node.get('result', {})
    data = result.get('data', {}) if isinstance(result, dict) else {}
    operation = data.get('operation') if isinstance(data, dict) else None
    if not isinstance(operation, dict): return False
    identifier = node.get('operation_id')
    return (node.get('tool') in kinds and isinstance(identifier, str) and bool(OP_ID.fullmatch(identifier))
            and operation.get('operation_id') == identifier and operation.get('kind') == kinds[node['tool']]
            and operation.get('status') == 'PENDING'
            and ((operation.get('required_verification') == 'STRONG')
                 == (node.get('state') == 'WAITING_VERIFICATION')))


def cny_evidence_sources(trace):
    """Direct business results or latest current successful task-node envelopes."""
    sources, tasks = [], {}
    task_tools = {'task_status', 'submit_task_plan', 'replan_task'}
    valid_id = lambda value: isinstance(value, str) and bool(re.fullmatch(r'[a-f0-9]{32}', value))
    for record in trace:
        if not isinstance(record, dict) or record.get('kind') != 'response': continue
        tool = record.get('tool')
        envelope = record.get('response')
        envelope = envelope if isinstance(envelope, dict) else {}
        usable = record.get('ok') is True and envelope.get('scope', 'bank') == 'bank'
        if tool in TASK_NODE_TOOLS and usable:
            sources.append((tool, envelope))
        if tool not in task_tools: continue
        data = envelope.get('data', {})
        task = data.get('task') if isinstance(data, dict) else None
        identifier = task.get('task_id') if isinstance(task, dict) else None
        args = record.get('args')
        requested = args.get('task_id') if isinstance(args, dict) else None
        usable = (usable and envelope.get('currency') == 'CNY' and envelope.get('amount_unit') == 'minor'
                  and valid_id(identifier) and (requested is None or requested == identifier))
        for key in {x for x in (identifier, requested) if valid_id(x)}:
            tasks[key] = task if usable and key == identifier else None
    for identifier, task in tasks.items():
        if (not isinstance(task, dict) or task.get('scope') != 'bank' or task.get('subject_id') != 'U1'
                or type(task.get('plan_version')) is not int or task['plan_version'] < 1): continue
        nodes = task.get('nodes')
        if not isinstance(nodes, list) or any(not isinstance(node, dict) for node in nodes): continue
        ids = [node.get('id') for node in nodes]
        if any(not isinstance(x, str) or not x for x in ids) or len(set(ids)) != len(ids): continue
        for node in nodes:
            if (node.get('tool') not in TASK_NODE_TOOLS or node.get('state') not in {
                    'SUCCEEDED', 'WAITING_CONFIRMATION', 'WAITING_VERIFICATION'}
                    or type(node.get('attempt')) is not int or node['attempt'] < 1
                    or node.get('error') is not None): continue
            key = node.get('execution_key')
            expected = r'task:' + identifier + r':v([1-9][0-9]*):' + re.escape(node['id'])
            execution = re.fullmatch(expected, key) if isinstance(key, str) else None
            if not (execution and int(execution[1]) <= task['plan_version'] and isinstance(node.get('result'), dict)):
                continue
            if node['state'] != 'SUCCEEDED' and not pending_cny_node(node):
                continue
            sources.append((node['tool'], node['result']))
    return sources


def cny_amount_values(trace):
    amounts, deficits, policies = set(), set(), set()
    for tool, envelope in cny_evidence_sources(trace):
        if (envelope.get('scope', 'bank') != 'bank' or envelope.get('currency') != 'CNY'
                or envelope.get('amount_unit') != 'minor'): continue
        raw_data = envelope.get('data', {})
        data = currency_clean_data(raw_data, forecast_currency_scopes(tool, raw_data))
        calculation = tool == 'money_calculate'
        if calculation:
            data = data.get('calculation', {}).get('result', {}) if isinstance(data, dict) else {}
        collect_values(data, amounts, set(), deficits, set(), policies, set(), calculation=calculation)
    return amounts, deficits, policies


def yuan_assumption(text, mention):
    """A plain direct yuan hypothesis, not an unknown decimal or foreign unit."""
    match = mention['match']
    if not re.match(r'[ \t]*元', text[match.end():]): return False
    prefix = re.split(r'[，,。；;！？!?\n]', text[:match.start()])[-1]
    return not re.search(r'(?:[A-Z]{3}|美元|欧元|日元|港币|港元|未知币种|币种未知|[$€£¥￥])[ \t]*(?:[:：][ \t]*)?$', prefix)


def chinese_percentages(text):
    """Yield a finite set of exact 成数 spellings, not arbitrary Chinese numbers."""
    expression = re.compile(
        r'(?<![零一二两三四五六七八九十百千万0-9负+−-])'
        r'(?P<sign>负|[-−+])?(?P<ratio>半成|[一二两三四五六七八九十]成[半一二三四五六七八九]?)'
        r'(?![半零一二两三四五六七八九十百千万0-9]|本|功|为|立|熟|交|员|果|长|形|色|品|就|不变)'
    )
    digits = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
              '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
    for match in expression.finditer(text):
        token = match.group('ratio')
        value = 5 if token == '半成' else digits[token[0]] * 10
        if token != '半成' and len(token) == 3:
            value += 5 if token[2] == '半' else digits[token[2]]
        if match.group('sign') in {'负', '-', '−'}:
            value = -value
        yield match.group(), Decimal(value)


def successful_responses(trace):
    return [item for item in trace if isinstance(item, dict)
            and item.get('kind') == 'response' and item.get('ok') is True
            and isinstance(item.get('response'), dict)
            and item['response'].get('scope', 'bank') == 'bank']


def collect_values(value, amounts, percentages, negative_balances=None, negative_percentages=None, policy_amounts=None, policy_percentages=None, *, calculation=False):
    """Only typed numeric fields count; descriptions and reference strings do not."""
    if isinstance(value, list):
        for item in value:
            collect_values(item, amounts, percentages, negative_balances, negative_percentages, policy_amounts, policy_percentages, calculation=calculation)
    elif isinstance(value, dict):
        for key, item in value.items():
            if key in {'args', 'arguments', 'inputs', 'display'}:
                continue
            if key == 'analysis_policy':
                if policy_amounts is not None and policy_percentages is not None:
                    collect_values(item, policy_amounts, policy_percentages, calculation=True)
                continue
            if key == 'calculation' and isinstance(item, dict):
                collect_values(item.get('result', {}), amounts, percentages, negative_balances, negative_percentages,
                               policy_amounts, policy_percentages, calculation=True)
                continue
            if key.endswith('_minor') and type(item) is int:
                amount = Decimal(item) / 100
                amounts.add(amount)
                is_balance = key == 'balance_minor' or key.endswith('_balance_minor')
                signed_result = calculation and key == 'amount_minor'
                if (is_balance or signed_result) and amount < 0 and negative_balances is not None:
                    # Magnitude is only for explicit deficit wording below, never
                    # an observed positive balance or an extra calculated value.
                    negative_balances.add(-amount)
                # Outflow magnitudes may be displayed as a positive expense.
                if not calculation and not is_balance:
                    amounts.add(abs(amount))
            elif (key.endswith('_percent') or key in {'percent_change', 'result_percent', 'percent'}) and isinstance(item, (int, float, str)) and not isinstance(item, bool):
                try:
                    percent = number(str(item))
                    percentages.add(percent)
                    if not calculation and key in {'change_percent', 'percent_change'} and percent < 0 and negative_percentages is not None:
                        negative_percentages.add(-percent)
                except InvalidOperation:
                    pass
            elif isinstance(item, (dict, list)):
                deficit_values = negative_balances
                if key == 'result' and value.get('tool') == 'money_calculate' and value.get('state') != 'SUCCEEDED':
                    # A task node's failed/pending payload is not a completed
                    # calculation for the new positive-deficit display rule.
                    deficit_values = None
                collect_values(item, amounts, percentages, deficit_values, negative_percentages, policy_amounts, policy_percentages, calculation=calculation)


def account_shortfall_prefix(prefix):
    """Recognize an account subject directly followed by the shortfall verb.

    A comparison between accounts is not a shortfall. This predicate supplies
    wording only; the caller must still require the exact negative result.
    """
    # The evidence belongs to this occurrence, not a denial in a prior clause.
    local = re.split(r'[，,。；;！？\n]', prefix)[-1]
    match = re.search(r'账户差(?:为|是|约|有|：|:|\s)*$', local)
    if not match:
        return False
    subject = local[:match.start()]
    if (NON_ASSERTION.search(subject)
            or re.search(r'不是|并非|不意味着|不存在|无需|不用|不必', subject)):
        return False
    # Detect plural subjects without enumerating savings/payment account types.
    comparison = r'两|多个|若干|不同|双方|各|这些|那些|这对|那对|比较|(?:[2-9]\d*|[二三四五六七八九十百]+)(?:个|人|方|类|种|家)'
    return not re.search(comparison, subject)


def deficit_suffix(text, match):
    """A local amount + yuan + deficit noun, not a positive financial fact."""
    suffix = text[match.end():].replace('*', '').replace('`', '')
    if not re.match(r'[ \t]*元[ \t]*(?:的[ \t]*)?缺口(?=$|[，,。；;！？!?、\s）)])', suffix):
        return False
    prefix = re.split(r'[，,。；;！？!?\n]', text[:match.start()])[-1]
    prefix = prefix.replace('*', '').replace('`', '')
    prefix = prefix.strip(' \t')
    # A new allowance requires a complete positive introduction, not the
    # absence of a growing list of incompatible financial verbs.
    if NON_ASSERTION.search(prefix) or re.search(r'并非|不是|否认|不再|无需|无须|是否', prefix):
        return False
    return (prefix in ('', '有')
            or re.fullmatch(r'(?:.{1,96}后[ \t]*)?(?:再|再次)?出现', prefix) is not None)


def deficit_display(text, match, negative_balances):
    """Display a negative balance or typed calculation result as a deficit.

    This local display conversion is not a calculation of a new balance. It does
    not establish which account/date the sentence means; semantic review remains.
    """
    if number(match.group()) not in negative_balances:
        return False
    prefix = text[max(0, match.start()-24):match.start()].replace('*', '').replace('`', '')
    return (bool(re.search(r'(?:缺口|短缺|还缺|缺|资金不足|仍差|还差|尚差|(?<![\u4e00-\u9fff])差)(?:为|是|约|有|：|:|\s)*$', prefix))
            or account_shortfall_prefix(prefix)
            or deficit_suffix(text, match))


def decline_display(text, match, negative_percentages):
    """A reported negative change may be displayed as a local decline magnitude."""
    if number(match.group()) not in negative_percentages:
        return False
    prefix = text[max(0, match.start()-24):match.start()].replace('*', '').replace('`', '')
    return bool(re.search(r'(?:下降|降低|减少|降幅|跌幅)(?:了|为|是|约|达|：|:|\s)*$', prefix))


def suffix_policy_reference(text, match):
    """Recognize only a nearby unit + policy noun with a finite policy context.

    Commas separate a previous observed value from this policy reference. Neither
    a policy noun elsewhere nor a direct balance/rate assertion grants support.
    """
    clean = lambda part: re.sub(r'[\s*`]', '', part)
    suffix = clean(text[match.end():match.end()+40])
    if not re.match(r'^(?:[%％]|元)(?:的)?(?:规则)?阈值(?=$|[，,。；;：:、！？)）\]】]|而触发)', suffix):
        return False
    prefix = clean(re.split(r'[。；;\n，,、]', text[max(0, match.start()-80):match.start()])[-1])
    if re.search(r'(?:实际|本期|本次|真实|当前).{0,6}(?:涨幅|增长率|百分比|增幅|金额|余额).{0,8}$', prefix):
        return False
    return not prefix or bool(re.search(r'(?:超过|高于|低于|未超过|未达到|采用|使用|参照|依据|按)$', prefix))


def policy_display(text, match, values):
    """Typed policy values support a policy explanation, not observed amounts/rates.

    This is a finite local-language discriminator, not full semantic verification.
    """
    if number(match.group()) not in values:
        return False
    if suffix_policy_reference(text, match):
        return True
    prefix = re.split(r'[。；;\n]', text[max(0, match.start()-100):match.start()])[-1]
    suffix = re.split(r'[。；;\n]', text[match.end():match.end()+60])[0]
    if re.search(r'(?:实际|本期|本次|真实).{0,6}(?:涨幅|增长率|百分比|增幅|金额|余额).{0,8}$', prefix):
        return False
    if not re.search(r'规则|阈值|标准', prefix + suffix):
        return False
    local = re.sub(r'[\s*`]', '', prefix)
    return bool(re.search(
        r'(?:增幅|涨幅|百分比|上升幅度|上升|绝对增加|绝对涨幅)(?:至少|不低于|≥|>=)$'
        r'|(?:阈值|标准)(?:为|是|至少|不低于|：|:)$'
        r'|(?:规则阈值|规则标准)(?:为|是|：|:)(?:涨幅|增幅|升幅|百分比)(?:达到|达|至少)?$'
        r'|(?:规则阈值|规则标准)[（(]至少$', local))


def collect_entities(value, operations, tasks):
    if isinstance(value, list):
        for item in value:
            collect_entities(item, operations, tasks)
    elif isinstance(value, dict):
        # An operation object needs a kind/status, not just an arbitrary ID.
        if value.get('operation_id') and value.get('kind') in {'transfer', 'mandate_query', 'mandate_cancel', 'arrangement'} and value.get('status'):
            operations[value['operation_id']] = value
        if value.get('task_id') and isinstance(value.get('nodes'), list) and value.get('state'):
            tasks[value['task_id']] = value
        for item in value.values():
            if isinstance(item, (dict, list)):
                collect_entities(item, operations, tasks)


def operation_kind(clause):
    # Merchant service termination is not proved by a bank mandate receipt.
    if re.search(r'(?:商户服务|会员服务|会员合同|商户合同)(?:已|被|成功|正式|经){0,6}(?:取消|撤销|终止)', clause):
        return 'merchant_service_cancel'
    if re.search(r'(?:代扣|扣款)授权(?:已|被|成功|正式|经|予以){0,6}(?:取消|撤销)', clause) or re.search(r'(?:取消|撤销)(?:了)?(?:银行)?(?:代扣|扣款)授权', clause):
        return 'mandate_cancel'
    if '撤销' in clause or '取消代扣' in clause:
        return 'mandate_cancel'
    if (re.search(r'(?:第[一二三四五六七八九十百0-9]+步)?查询已(?:经)?(?:准备|备好)', clause)
            and WAIT_CONFIRM.search(clause)):
        return 'mandate_query'
    if '代扣' in clause or '授权清单' in clause or '查询卡' in clause:
        return 'mandate_query'
    if '转账' in clause or '划转' in clause or '调拨' in clause:
        return 'transfer'
    if '安排' in clause:
        return 'arrangement'
    return None


def quoted_action_label(match):
    """Preserve exact action labels, never quoted status assertions/task titles."""
    label = match.group()[1:-1].strip()
    action_labels = {'代扣查询', '查询代扣', '撤销代扣', '取消代扣',
                     '转账', '划转', '调拨', '新增安排', '修改安排'}
    return label if label in action_labels else ''


def requests_details_before_future_preparation(clause):
    """A narrow request-then-future plan is not an assertion of a prepared card."""
    if re.search(r'已|成功|完成|好了', clause):
        return False
    # The new amount must be requested before this bounded future replan offer.
    if re.fullmatch(
        r'(?:[1-9][0-9]?[.)、][ \t]*)?(?:请(?:您)?(?:先)?)?'
        r'(?:告知|提供|明确)新的转账金额[，,][ \t]*'
        r'我(?:会|将|再)?重规划任务并重新准备(?:待确认操作|转账)', clause):
        return True
    return bool(re.search(
        r'(?:告诉我|(?:请(?:您)?(?:先)?)?(?:提供|明确|指定)).{0,36}'
        r'(?:金额|数额|账户|日期).{0,40}[，,]\s*'
        r'我(?:会|再|将|立即|据此|按此){1,3}(?:重新|再)?准备[^，,。；;！？\n]{0,32}$', clause))


def explains_verification_standard(clause):
    """A complete whether/receipt criterion is not a claim of execution.

    Match the entire clause, so a criterion cannot hide another success claim.
    """
    if re.search(r'已|好了|办妥|(?:入账|生效|完成|成功)了', clause):
        return False
    return bool(re.fullmatch(
        r'(?:转账|撤销|取消代扣|查询|银行操作|操作|授权|安排|任务)?'
        r'是否(?:生效|完成|成功)[，,]?\s*(?:应|需|须)?以'
        r'[^，,。；;！？\n]{0,40}(?:回执|状态)[^，,。；;！？\n]{0,20}为准', clause))


def offers_optional_future_service(clause):
    """A bounded future offer still waits for the user's later request/confirmation."""
    if re.search(r'已|成功|完成|好了|办妥|当前|现在|本次', clause):
        return False
    return bool(re.fullmatch(
        r'(?:如|若)(?:后续|以后)(?:您|你)?(?:还)?(?:想|需要)'
        r'[^。；;！？\n]{0,90}(?:仍|届时)?(?:会|将|再)'
        r'[^，,。；;！？\n]{0,14}(?:等|请)[^，,。；;！？\n]{0,8}确认', clause))


def explains_receipt_boundary(clause):
    """A whole conceptual scope statement does not assert an actual receipt."""
    return bool(re.fullmatch(
        r'(?:银行操作)?(?:成功)?回执(?:也)?(?:只|仅)(?:能)?证明'
        r'(?:银行)?(?:代扣|扣款)授权状态[，,]'
        r'不(?:能)?证明(?:商户服务|会员服务|会员合同|商户合同)已(?:取消|撤销|终止)', clause))


def asserted_action_text(clause):
    """Remove only a local non-proof statement, not nearby actual assertions.

    A bank success and its merchant-service limitation may share one clause.
    Keep the bank assertion (and any second merchant assertion) for receipt checks.
    """
    return re.sub(
        r'(?:并)?不(?:能)?证明(?:商户服务|会员服务|会员合同|商户合同)'
        r'已(?:被)?(?:成功)?(?:取消|撤销|终止)', '', clause)


def readonly_completion(clause, tasks):
    """Return None for other wording, else verify typed nodes in one task.

    This finite statement names both a read-only tool family and exact count.
    Task/operation completion remains separately validated, never inferred here.
    """
    match = re.fullmatch(
        r'(?:[0-9]+[.)、]\s*)?(?:评估|分析|计算)已完成[（(]'
        r'(?P<count>[1-9]|一|二|两|三|四|五|六|七|八|九)个'
        r'(?P<family>预测|账单分析|周期识别|计算)节点(?:均|都)已(?:成功|完成)[）)]', clause)
    if match is None:
        return None
    counts = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5,
              '六': 6, '七': 7, '八': 8, '九': 9}
    token = match.group('count')
    count = int(token) if token.isdigit() else counts[token]
    tool = {'预测': 'forecast', '账单分析': 'billing_report',
            '周期识别': 'recurring_candidates', '计算': 'money_calculate'}[match.group('family')]
    for task in tasks.values():
        nodes = [n for n in task['nodes'] if n.get('tool') == tool]
        if len(nodes) == count and all(
                n.get('state') == 'SUCCEEDED' and not n.get('operation_id')
                and isinstance(n.get('result'), dict)
                and isinstance(n['result'].get('data'), dict)
                and bool(n['result']['data']) for n in nodes):
            return True
    return False


def readonly_node_result(tool, result):
    """Check the actual read tool's result shape, not an arbitrary success label."""
    if (not isinstance(result, dict) or result.get('scope', 'bank') != 'bank'
            or not isinstance(result.get('evidence_id'), str) or not result['evidence_id'].strip()):
        return False
    data = result.get('data')
    if not isinstance(data, dict) or not data:
        return False
    list_field = {'accounts': 'accounts', 'transactions': 'transactions', 'arrangements': 'arrangements',
                  'recurring_candidates': 'candidates', 'insight_status': 'candidates'}.get(tool)
    if list_field:
        rows = data.get(list_field)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            return False
        return (type(data.get('total_balance_minor')) is int if tool == 'accounts' else
                isinstance(data.get('notifications'), list) if tool == 'insight_status' else True)
    if tool == 'spending_summary':
        return all(type(data.get(key)) is int for key in ('expense_minor', 'income_minor', 'refund_minor'))
    if tool == 'forecast':
        return (isinstance(data.get('through_date'), str) and isinstance(data.get('daily'), list)
                and type(data.get('current_balance_minor')) is int)
    if tool == 'billing_report':
        report = data.get('report')
        return (isinstance(report, dict) and isinstance(report.get('totals'), dict)
                and type(report['totals'].get('count')) is int
                and isinstance(report.get('start_date'), str) and isinstance(report.get('end_date'), str))
    if tool == 'money_calculate':
        calculation = data.get('calculation')
        value = calculation.get('result') if isinstance(calculation, dict) else None
        return (isinstance(value, dict) and
                ((value.get('kind') == 'amount' and type(value.get('amount_minor')) is int)
                 or (value.get('kind') == 'percentage' and isinstance(value.get('percent'), (str, int, float))
                     and not isinstance(value.get('percent'), bool))))
    return False


def successful_readonly_node(task, tool, node_id):
    """Select from all current nodes before testing success; never a subset."""
    if not isinstance(task, dict) or task.get('scope') != 'bank' or task.get('subject_id') != 'U1':
        return False
    nodes = task.get('nodes')
    if not isinstance(nodes, list) or not nodes or any(not isinstance(node, dict) for node in nodes):
        return False
    identifiers = [node.get('id') for node in nodes]
    if (any(not isinstance(identifier, str) or not identifier for identifier in identifiers)
            or len(set(identifiers)) != len(identifiers)):
        return False
    candidates = [node for node in nodes if node.get('tool') == tool
                  and (node_id is None or node['id'] == node_id)]
    if len(candidates) != 1:
        return False
    node = candidates[0]
    return (node.get('state') == 'SUCCEEDED' and type(node.get('attempt')) is int and node['attempt'] >= 1
            and not node.get('operation_id') and node.get('error') is None
            and readonly_node_result(tool, node.get('result')))


def remove_verified_readonly_node_success(text, status_tasks):
    """Mask only the supported node-success predicate, retaining all neighbours."""
    families = {'余额查询': 'accounts', '账户查询': 'accounts', '账户核对': 'accounts',
                '流水查询': 'transactions', '支出统计': 'spending_summary',
                '安排查询': 'arrangements', '安排核对': 'arrangements',
                '现金流预测': 'forecast', '基准预测': 'forecast', '拟消费预测': 'forecast',
                '账单分析': 'billing_report', '账单查询': 'billing_report',
                '周期识别': 'recurring_candidates', '关注状态查询': 'insight_status',
                '金额计算': 'money_calculate'}
    expression = re.compile(
        r'(?P<outer>并不是|不是|并非)?(?P<label>' + '|'.join(families) + r')\s*节点'
        r'(?:\s*[（(](?P<node>[A-Za-z][A-Za-z0-9_]{0,39})[）)]'
        r'|\s+(?P<bare_node>[A-Za-z][A-Za-z0-9_]{0,39}))?\s*[:：]?\s*'
        r'(?:(?:保留|沿用)(?:原有?|已有)?)?\s*'
        r'(?P<claim>已(?:经)?(?:成功|完成)|(?:并不是|不是|并非)?未(?:成功|完成))'
        r'(?:结果)?(?:\s*[（(](?P<after_node>[A-Za-z][A-Za-z0-9_]{0,39})[）)])?')
    identifiers = set(re.findall(r'\b[a-f0-9]{32}\b', text))
    selected = (status_tasks.get(next(iter(identifiers))) if len(identifiers) == 1 else
                next(iter(status_tasks.values())) if not identifiers and len(status_tasks) == 1 else None)
    quotes = [(m.start(), m.end()) for m in re.finditer(r'「[^」]*」|“[^”]*”|"[^"\n]*"', text)]
    reasons = []

    def checked(match):
        if any(start <= match.start() < end for start, end in quotes):
            return match[0]
        claim = match['claim']
        double = (bool(match['outer']) and claim.startswith('未')) or bool(re.match(r'(?:并不是|不是|并非)未', claim))
        prefix = re.split(r'[，,。；;：:！？!?\n]', text[:match.start()])[-1]
        if not double and (match['outer'] or claim.startswith('未') or NON_ASSERTION.search(prefix)
                           or re.search(r'是否\s*$', prefix)):
            return match[0]
        node_ids = {value for value in (match['node'], match['bare_node'], match['after_node']) if value}
        valid = len(node_ids) <= 1 and successful_readonly_node(
            selected, families[match['label']], next(iter(node_ids)) if node_ids else None)
        if not valid:
            reasons.append({'code': 'UNBACKED_READONLY_NODE_SUCCESS', 'clause': match[0]})
        # Even an invalid claim is replaced after adding its own reason, so a
        # bank operation elsewhere cannot accidentally provide its evidence.
        begin, end = match.start('claim') - match.start(), match.end('claim') - match.start()
        return match[0][:begin] + ' ' * (end - begin) + match[0][end:]

    return expression.sub(checked, text), reasons


def attempted_preparation_node(task, tool, node_id):
    """Return a current node whose execution epoch has a real start audit."""
    if (not isinstance(task, dict) or task.get('scope') != 'bank'
            or task.get('subject_id') != 'U1'
            or task.get('state') not in {'READY', 'RUNNING', 'FAILED', 'WAITING_CONFIRMATION', 'WAITING_VERIFICATION', 'SUCCEEDED', 'CANCELLED', 'PAUSED', 'NEEDS_HUMAN'}):
        return None
    identifier, version = task.get('task_id'), task.get('plan_version')
    if (not isinstance(identifier, str) or not re.fullmatch(r'[a-f0-9]{32}', identifier)
            or type(version) is not int or version < 1):
        return None
    nodes, audit = task.get('nodes'), task.get('audit')
    if (not isinstance(nodes, list) or not nodes or any(not isinstance(n, dict) for n in nodes)
            or not isinstance(audit, list)):
        return None
    ids = [n.get('id') for n in nodes]
    if any(not isinstance(i, str) or not re.fullmatch(r'[A-Za-z][A-Za-z0-9_]{0,39}', i) for i in ids) or len(set(ids)) != len(ids):
        return None
    candidates = [n for n in nodes if n.get('tool') == tool and (node_id is None or n['id'] == node_id)]
    if len(candidates) != 1:
        return None
    node = candidates[0]
    if (type(node.get('attempt')) is not int or node['attempt'] < 1
            or node.get('state') not in {'RUNNING', 'WAITING_CONFIRMATION', 'WAITING_VERIFICATION', 'SUCCEEDED', 'FAILED', 'CANCELLED'}):
        return None
    execution = re.fullmatch(r'task:' + identifier + r':v([1-9][0-9]*):' + re.escape(node['id']), str(node.get('execution_key', '')))
    if execution is None or int(execution[1]) > version:
        return None
    epoch, sequence, matched = None, 0, False
    for entry in audit:
        if (not isinstance(entry, dict) or entry.get('task_id') != identifier
                or type(entry.get('sequence')) is not int or entry['sequence'] <= sequence
                or not isinstance(entry.get('details'), dict)):
            return None
        sequence, details = entry['sequence'], entry['details']
        if entry.get('event') in {'SUBMITTED', 'REPLANNED'}:
            next_epoch = details.get('plan_version')
            if type(next_epoch) is not int or next_epoch < 1 or (epoch is not None and next_epoch <= epoch):
                return None
            epoch = next_epoch
        if (entry.get('event') == 'NODE_STARTED' and epoch == int(execution[1])
                and details.get('node_id') == node['id'] and type(details.get('attempt')) is int
                and details['attempt'] == node['attempt']):
            matched = True
    if epoch != version or not matched:
        return None
    operation_id = node.get('operation_id')
    if operation_id is not None and (not isinstance(operation_id, str) or not re.fullmatch(r'op_[A-Za-z0-9]+', operation_id)):
        return None
    return node


def task_stage_progress_spans(clause, status_tasks, text):
    """Finite stage predicates prove an attempt, never a prepared bank action."""
    stages = {'转账准备': ('prepare_transfer', 'transfer'),
              '代扣查询准备': ('prepare_mandate_query', 'mandate_query'),
              '授权查询准备': ('prepare_mandate_query', 'mandate_query'),
              '代扣撤销准备': ('prepare_mandate_cancel', 'mandate_cancel'),
              '授权撤销准备': ('prepare_mandate_cancel', 'mandate_cancel')}
    expression = re.compile(
        r'任务(?:[ \t]+(?P<task>[a-f0-9]{32}))?[ \t]*'
        r'(?:已(?:建立|保存|创建)并|已)?(?:执行到|推进到|运行到)'
        r'(?P<stage>' + '|'.join(stages) + r')(?:这?一步|阶段|节点)'
        r'(?:[ \t]*[（(](?P<node>[A-Za-z][A-Za-z0-9_]{0,39})[）)])?')
    identifiers = set(re.findall(r'\b[a-f0-9]{32}\b', text))
    selected = (status_tasks.get(next(iter(identifiers))) if len(identifiers) == 1 else
                next(iter(status_tasks.values())) if not identifiers and len(status_tasks) == 1 else None)
    spans = []
    for match in expression.finditer(clause):
        prefix = re.split(r'[，,。；;：:！？!?\n]', clause[:match.start()])[-1]
        if NON_ASSERTION.search(prefix) or re.search(r'未|没有|并非|不是|是否', prefix):
            continue
        tool, kind = stages[match['stage']]
        node = attempted_preparation_node(selected, tool, match['node'])
        spans.append({'start': match.start(), 'end': match.end(), 'tool': tool, 'kind': kind,
                      'supported': node is not None,
                      'task_id': selected.get('task_id') if isinstance(selected, dict) else None,
                      'node_id': node['id'] if node else None,
                      'operation_id': node.get('operation_id') if node else None})
    return spans


def stage_readonly_completion(text, task):
    """A separate named read-only result is not the pending bank operation."""
    families = {'余额查询': 'accounts', '账户查询': 'accounts',
                '流水查询': 'transactions', '账单分析': 'billing_report'}
    match = re.fullmatch(r'(?:已完成(?P<after>' + '|'.join(families)
                        + r')|(?P<before>' + '|'.join(families) + r')已完成)', text.strip())
    if not match:
        return None
    label = match['after'] or match['before']
    return successful_readonly_node(task, families[label], None)


def current_status_tasks(trace):
    """A latest failed/foreign lookup invalidates summary evidence, not bank state.

    A direct task ID (or request locator on a failed response) only identifies
    which evidence to invalidate. Request arguments never establish completion.
    """
    tasks = {}
    for record in trace:
        if not isinstance(record, dict) or record.get('kind') != 'response' or record.get('tool') != 'task_status':
            continue
        response = record.get('response')
        response = response if isinstance(response, dict) else {}
        data = response.get('data')
        task = data.get('task') if isinstance(data, dict) else None
        identifier = task.get('task_id') if isinstance(task, dict) else None
        arguments = record.get('args')
        requested = arguments.get('task_id') if isinstance(arguments, dict) else None
        valid_id = lambda value: isinstance(value, str) and bool(re.fullmatch(r'[a-f0-9]{32}', value))
        identities = {value for value in (identifier, requested) if valid_id(value)}
        usable = (record.get('ok') is True and response.get('scope', 'bank') == 'bank'
                  and valid_id(identifier) and (requested is None or requested == identifier))
        for key in identities:
            tasks[key] = task if usable and key == identifier else None
    return tasks


def completed_task_nodes(task):
    """Return the complete successful node list, never just successful subsets."""
    if not isinstance(task, dict) or task.get('scope') != 'bank' or task.get('subject_id') != 'U1':
        return None
    nodes = task.get('nodes')
    if task.get('state') != 'SUCCEEDED' or not isinstance(nodes, list) or not 1 <= len(nodes) <= 20:
        return None
    identifiers = set()
    prepare_kinds = {'prepare_transfer':'transfer', 'prepare_mandate_query':'mandate_query',
                     'prepare_mandate_cancel':'mandate_cancel', 'propose_arrangement':'arrangement'}
    for node in nodes:
        if not isinstance(node, dict) or node.get('state') != 'SUCCEEDED':
            return None
        identifier = node.get('id')
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            return None
        identifiers.add(identifier)
        result = node.get('result')
        if not isinstance(node.get('tool'), str) or node['tool'] not in TASK_NODE_TOOLS or not isinstance(result, dict):
            return None
        data = result.get('data')
        if result.get('scope', 'bank') != 'bank' or not isinstance(data, dict) or not data:
            return None
        if node['tool'] in prepare_kinds:
            operation = data.get('operation')
            if not isinstance(operation, dict):
                return None
            oid = node.get('operation_id')
            receipt = operation.get('receipt')
            if (not isinstance(oid, str) or not oid or operation.get('operation_id') != oid
                    or operation.get('kind') != prepare_kinds[node['tool']]
                    or operation.get('status') != 'SUCCEEDED' or not isinstance(receipt, dict)
                    or receipt.get('operation_id') != oid or receipt.get('kind') != operation['kind']
                    or receipt.get('status') != 'SUCCEEDED' or not receipt.get('receipt_id')):
                return None
    return nodes


def task_node_labels_match(labels, nodes):
    """Labels are neutral tool names, not an arbitrary parenthesized assertion."""
    if labels is None:
        return True
    mapping = {'账户核对':'accounts', '账户查询':'accounts', '安排核对':'arrangements',
               '安排查询':'arrangements', '基准预测':'forecast', '拟消费预测':'forecast',
               '现金流预测':'forecast', '账单分析':'billing_report', '周期识别':'recurring_candidates',
               '金额计算':'money_calculate', '转账办理':'prepare_transfer',
               '代扣查询':'prepare_mandate_query', '代扣撤销':'prepare_mandate_cancel',
               '安排修改':'propose_arrangement', '回执核对':'operation'}
    names = [name.strip() for name in re.split(r'[、，,]', labels)]
    if any(name not in mapping for name in names):
        return False
    return sorted(mapping[name] for name in names) == sorted(node['tool'] for node in nodes)


def remove_verified_task_summary(text, status_tasks):
    """Check and remove only a bounded task summary; preserve neighbouring claims.

    An invalid summary also gets removed after recording its own rejection, so
    its local '没有' never suppresses another action assertion in the sentence.
    """
    expression = re.compile(
        r'任务\s*(?:(?P<id>[a-f0-9]{32})\s*[:：]?\s*)?'
        r'(?P<count>[0-9]{1,2})\s*个节点\s*'
        r'(?:[（(](?P<labels>[^（）()。；;！？\n]{1,180})[）)]\s*)?'
        r'(?:均|都)已(?:成功|完成)\s*[，,]\s*'
        r'(?:无|没有)失败(?:或|及|和)待确认节点')
    reasons = []
    def checked(match):
        prefix = re.split(r'[。！？；\n]', text[:match.start()])[-1]
        if re.search(r'(?:如果|若)\s*$', prefix):
            return match.group()  # Preserve the existing conditional-statement handling.
        identifier = match.group('id')
        candidates = ([status_tasks.get(identifier)] if identifier else
                      list(status_tasks.values()) if len(status_tasks) == 1 else [])
        nodes = completed_task_nodes(candidates[0]) if len(candidates) == 1 else None
        valid = (nodes is not None and len(nodes) == int(match.group('count'))
                 and task_node_labels_match(match.group('labels'), nodes))
        if not valid:
            reasons.append({'code':'UNBACKED_TASK_COMPLETION', 'clause':match.group()})
        tail = text[match.end():match.end()+45]
        if re.match(r'^[，,]\s*(?:但(?:是)?|而|且)?\s*(?:仍|还)(?:有|存在)'
                    r'(?:[0-9]+\s*个)?(?:失败|待确认|等待确认)(?:节点)?', tail):
            reasons.append({'code':'CONTRADICTORY_TASK_STATE', 'clause':match.group()+tail})
        return ''
    return expression.sub(checked, text), reasons


def remove_conditional_reconcile_advice(text):
    """Remove a bounded future match suggestion, never a present prepared action.

    The UI offers reconciliation only when a candidate exists. This wording
    neither proves that a candidate exists now nor guarantees a future match.
    Adjacent action assertions remain in the returned text for normal checks.
    """
    event = r'(?:(?:项目)?收入(?:实际)?到账|(?:还款|支出)(?:实际)?扣款)'
    condition = (
        r'(?:(?:如|若|如果)' + event + r'(?:(?:或|和)' + event + r'){0,2}'
        r'(?:且出现待核对候选)?(?:后)?[，,]\s*|'
        + event + r'(?:且出现待核对候选)?后[，,]?\s*)')
    advice = (r'请(?:及时)?(?:在(?:核对页面|核对区域|核对区))?'
              r'(?:核对后)?确认(?:对应|相关)安排'
              r'(?:[，,]\s*(?:以(?:保持|确保)预测准确|(?:以)?避免重复预测))?')
    return re.sub(r'(?<!\w)' + condition + advice + r'(?=$|[，,。；;！？\n])', '', text)


def pending_dependent_step(task, ordinal, tool):
    """A described later step must still wait on a real unfinished dependency."""
    if (not isinstance(task, dict) or task.get('scope') != 'bank' or task.get('subject_id') != 'U1'
            or task.get('state') not in {'READY', 'PENDING', 'RUNNING', 'WAITING_CONFIRMATION', 'WAITING_VERIFICATION', 'PAUSED'}):
        return False
    nodes = task.get('nodes')
    if not isinstance(nodes, list) or not 1 <= len(nodes) <= 20 or not 1 <= ordinal <= len(nodes):
        return False
    indexed = {}
    for node in nodes:
        if (not isinstance(node, dict) or not isinstance(node.get('id'), str) or not node['id']
                or node['id'] in indexed or node.get('tool') not in TASK_NODE_TOOLS):
            return False
        indexed[node['id']] = node
    selected = nodes[ordinal - 1]
    dependencies = selected.get('depends_on')
    if (selected.get('state') != 'PENDING' or selected.get('tool') != tool
            or not isinstance(dependencies, list) or not dependencies
            or any(not isinstance(key, str) or key not in indexed or key == selected['id'] for key in dependencies)):
        return False
    states = [indexed[key].get('state') for key in dependencies]
    allowed = {'SUCCEEDED', 'PENDING', 'RUNNING', 'WAITING_CONFIRMATION', 'WAITING_VERIFICATION'}
    return all(state in allowed for state in states) and any(state != 'SUCCEEDED' for state in states)


def future_step_reference(task, ordinal, tool, dependency_tools, argument, suffix):
    """Return the actual dependency node, only for an exact direct result ref."""
    if not pending_dependent_step(task, ordinal, tool):
        return False
    node = task['nodes'][ordinal - 1]
    args = node.get('args')
    if not isinstance(args, dict):
        return False
    return next((dependency for dependency in task['nodes']
                 if dependency['id'] in node['depends_on']
                 and dependency['tool'] in dependency_tools
                 and args.get(argument) == {'$ref': dependency['id'] + suffix}), None)


def future_operation_binding(node):
    """Keep both kind and current identity; None means no operation is evidenced."""
    kinds = {'prepare_mandate_query': ('mandate_query', '代扣查询'),
             'prepare_mandate_cancel': ('mandate_cancel', '撤销操作'),
             'prepare_transfer': ('transfer', '转账操作'),
             'propose_arrangement': ('arrangement', '安排操作')}
    kind, label = kinds[node['tool']]
    identifier = node.get('operation_id')
    return {'kind': kind, 'label': label, 'node_id': node['id'],
            'operation_id': identifier if isinstance(identifier, str) and OP_ID.fullmatch(identifier) else None}


def future_step_prefix(body, task, ordinal):
    """Recognize a finite future action, returning only its removable span.

    No arbitrary title or trailing prose belongs to that span. A suffix remains
    subject to ordinary action checks, and numeric checks always see full text.
    """
    if not isinstance(task, dict) or task.get('state') not in {
            'READY', 'WAITING_CONFIRMATION', 'WAITING_VERIFICATION'}:
        return None
    boundary = r'(?=$|[，,。；;：:])'
    confirmation = r'(?:，|,)?(?:仍需|需要|并(?:再次)?等待)(?:您|你|本人)(?:本人)?(?:在页面)?确认'
    # A receipt must reference the prepare node named by its future condition.
    receipt = re.match(
        r'(?:(?:回执核对|核对回执|读取回执)[:：]\s*)?'
        r'(?P<kind>查询|转账|撤销|操作|安排修改)完成后(?:将)?(?:自动)?(?:读取|核对)(?:真实|银行)?回执'
        r'(?:[，,]核实(?P<state>授权|操作|转账|安排)状态后向(?:您|你)报告|[，,]向(?:您|你)报告结果)?' + boundary, body)
    if receipt:
        tools = {'查询': {'prepare_mandate_query'}, '转账': {'prepare_transfer'},
                 '撤销': {'prepare_mandate_cancel'}, '安排修改': {'propose_arrangement'},
                 '操作': {'prepare_mandate_query', 'prepare_transfer', 'prepare_mandate_cancel', 'propose_arrangement'}}
        states = {'授权': {'prepare_mandate_query', 'prepare_mandate_cancel'},
                  '转账': {'prepare_transfer'}, '安排': {'propose_arrangement'},
                  '操作': tools['操作']}
        allowed = tools[receipt['kind']]
        if receipt['state']:
            allowed = allowed & states[receipt['state']]
        dependency = future_step_reference(task, ordinal, 'operation', allowed,
                                           'operation_id', '.data.operation.operation_id')
        if dependency:
            return receipt.end(), future_operation_binding(dependency)
    match = re.match(
        r'(?:(?:授权匹配|匹配授权)[:：]\s*)?(?:待)?查询(?:确认|完成)后(?:将)?自动匹配(?:唯一)?(?:生效)?(?:代扣)?授权'
        + boundary, body)
    if match:
        dependency = future_step_reference(task, ordinal, 'match_mandate_from_query',
                                           {'prepare_mandate_query'}, 'query_operation_id', '.data.operation.operation_id')
        if dependency:
            return match.end(), future_operation_binding(dependency)
    cancel = re.match(
        r'(?:(?:撤销授权|撤销该授权|准备撤销)[:：]\s*)?匹配(?:成功|完成)后(?:会|将)?'
        r'(?:准备撤销(?:该|代扣)?授权|生成(?:一张)?撤销确认卡)' + confirmation + boundary, body)
    if cancel and future_step_reference(task, ordinal, 'prepare_mandate_cancel',
                                       {'match_mandate_from_query'}, 'mandate_id', '.data.match.selected.mandate_id'):
        return cancel.end(), future_operation_binding(task['nodes'][ordinal - 1])
    prepare = re.match(
        r'(?:(?:准备查询|准备转账|修改安排)[:：]\s*)?前序步骤完成后(?:会|将)?准备'
        r'(?P<label>查询代扣|转账|修改安排)' + confirmation + boundary, body)
    if prepare:
        tools = {'查询代扣': 'prepare_mandate_query', '转账': 'prepare_transfer', '修改安排': 'propose_arrangement'}
        if pending_dependent_step(task, ordinal, tools[prepare['label']]):
            return prepare.end(), future_operation_binding(task['nodes'][ordinal - 1])
    return None


def future_remainder_with_context(remainder, context):
    """Elliptical actual assertions retain the operation they refer back to."""
    output = []
    contexts = {'mandate_query': '代扣查询', 'mandate_cancel': '撤销操作',
                'transfer': '转账操作', 'arrangement': '安排操作',
                'merchant_service_cancel': '商户服务'}
    for part in re.split(r'([，,。；;：:！？])', remainder):
        kind = operation_kind(part)
        if kind:
            context = contexts[kind]
        elif re.match(r'\s*(?:已|现在|当前|等|待|请|操作)', part):
            part = context + part
        output.append(part)
    return ''.join(output)


def remove_identified_future_steps(text, status_tasks, *, suffix_validator=None):
    """Resolve one actual task and one future node; list numbers are presentation."""
    ids = set(re.findall(r'\b[a-f0-9]{32}\b', text))
    if len(ids) > 1 or (not ids and len(status_tasks) != 1):
        return text
    task = status_tasks.get(next(iter(ids))) if ids else next(iter(status_tasks.values()))
    if not isinstance(task, dict) or not isinstance(task.get('nodes'), list):
        return text
    output = []
    for line in text.splitlines(keepends=True):
        match = re.match(r'\s*([1-9][0-9]?)[.)、]\s*(.*?)(\r?\n)?$', line)
        matches = [end for ordinal in range(1, len(task['nodes']) + 1)
                   if (end := future_step_prefix(match[2], task, ordinal)) is not None] if match else []
        span = matches[0] if len(matches) == 1 else None
        if span is not None:
            end, binding = span
            remainder = match[2][end:]
            # Preserve current-state claims appended after the finite future span.
            # Keep bank context even when the remainder only says "已成功".
            remainder = (future_remainder_with_context(remainder, binding['label'])
                         if remainder.strip('，,。；;：: ') else '')
            if remainder and suffix_validator:
                suffix_validator(remainder, binding)
            output.append(remainder + (match[3] or ''))
        else:
            output.append(line)
    return ''.join(output)


def remove_verified_future_plan_steps(text, status_tasks, *, suffix_validator=None):
    """Remove only neutral numbered later steps within an evidenced saved plan."""
    text = remove_identified_future_steps(text, status_tasks, suffix_validator=suffix_validator)
    mentioned = set(re.findall(r'\b[a-f0-9]{32}\b', text))
    if mentioned:
        candidate = status_tasks.get(next(iter(mentioned))) if len(mentioned) == 1 else None
    else:
        candidate = next(iter(status_tasks.values())) if len(status_tasks) == 1 else None
    labels = {'查询代扣':'prepare_mandate_query', '查询银行代扣授权':'prepare_mandate_query',
              '撤销':'prepare_mandate_cancel', '撤销代扣授权':'prepare_mandate_cancel',
              '取消代扣授权':'prepare_mandate_cancel', '转账':'prepare_transfer', '修改安排':'propose_arrangement'}
    expression = re.compile(r'\s*(?P<count>[1-9][0-9]?)[.)、]\s*准备(?P<label>'
        + '|'.join(labels) + r')并(?:再次)?等待(?:本人|你|您)确认\s*')
    in_plan, saw_item = False, False
    output = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if re.fullmatch(r'已(?:经)?保存任务[^\n]{0,160}流程为[:：]', stripped):
            in_plan, saw_item = True, False
        elif in_plan and not stripped:
            if saw_item:
                in_plan = False
        elif in_plan:
            if not re.match(r'[1-9][0-9]?[.)、]\s*', stripped):
                in_plan = False
            else:
                saw_item = True
                match = expression.fullmatch(stripped)
                if match and pending_dependent_step(candidate, int(match['count']), labels[match['label']]):
                    output.append('\n' if line.endswith('\n') else '')
                    continue
        output.append(line)
    return ''.join(output)


def execution_claim_spans(clause):
    """Classify each positive match; a time condition proves no present success."""
    spans = []
    for match in POSITIVE_EXECUTE.finditer(clause):
        bare = re.fullmatch(r'(?P<action>转账|查询|撤销|取消|操作)(?:执行)?(?:成功|完成|办妥)', match[0])
        time_word = re.match(r'\s*(?:之后|以后|后)(?![果续台方面者退])', clause[match.end():]) if bare else None
        left = clause[:match.start()]
        actual_prefix = re.search(r'(?:已(?:经)?|现已)(?:将|把)?(?:该|这|本)?(?:笔|次|项)?\s*$', left)
        conditional = bool(bare and time_word and not actual_prefix)
        spans.append({'start': match.start(), 'end': match.end(), 'matched_text': match[0],
                      'classification': 'TEMPORAL_CONDITION' if conditional else 'ASSERTED',
                      'condition_end': match.end() + time_word.end() if conditional else None,
                      'action': bare['action'] if bare else None,
                      'reason': 'BARE_ACTION_WITH_POSTPOSED_TIME' if conditional else 'PRESENT_SUCCESS_CANDIDATE'})
    return spans


def execution_context_without_conditions(clause, spans):
    """Remove only a conditional status+connector, retaining its action subject."""
    output, position = [], 0
    for span in spans:
        if span['classification'] != 'TEMPORAL_CONDITION':
            continue
        output.extend([clause[position:span['start']], span['action']])
        position = span['condition_end']
    output.append(clause[position:])
    return ''.join(output)


def actual_execution_spans(clause, spans):
    """A local denial/question is not an assertion; it cannot erase a later one."""
    actual = []
    for span in spans:
        if span['classification'] != 'ASSERTED':
            continue
        before = clause[:span['start']]
        if re.search(r'是否\s*$', before[-8:]):
            continue
        start = max((before.rfind(separator) for separator in '，,：:；;。！？\n'), default=-1) + 1
        local = clause[start:span['end']]
        local_spans = execution_claim_spans(local)
        if NON_ASSERTION.search(execution_context_without_conditions(local, local_spans)):
            continue
        actual.append(span)
    return actual


def temporal_condition_binding(span, status_tasks, text):
    """Resolve the receipt dependency; syntax alone grants no new exemption."""
    ids = set(re.findall(r'\b[a-f0-9]{32}\b', text))
    if len(ids) > 1 or (not ids and len(status_tasks) != 1):
        return None
    task = status_tasks.get(next(iter(ids))) if ids else next(iter(status_tasks.values()))
    if (not isinstance(task, dict) or task.get('state') not in {
            'READY', 'WAITING_CONFIRMATION', 'WAITING_VERIFICATION'}
            or not isinstance(task.get('nodes'), list)):
        return None
    tools = {'查询': {'prepare_mandate_query'}, '转账': {'prepare_transfer'},
             '撤销': {'prepare_mandate_cancel'}, '取消': {'prepare_mandate_cancel'},
             '操作': {'prepare_mandate_query', 'prepare_mandate_cancel', 'prepare_transfer', 'propose_arrangement'}}
    matches = []
    for ordinal in range(1, len(task['nodes']) + 1):
        dependency = future_step_reference(task, ordinal, 'operation', tools[span['action']],
                                           'operation_id', '.data.operation.operation_id')
        if dependency:
            matches.append(future_operation_binding(dependency))
    return matches[0] if len(matches) == 1 else None


def action_clause_parts(text):
    """Keep whether a split ended the sentence, so nearby targets stay explicit."""
    expression = r'[。！？；\n]|[，,](?:但(?:是)?|并且|且)|[，,](?=(?:这|本|该|已|转账|查询|操作|任务|银行操作|当前(?:转账|查询|操作|任务)))|[，,](?=(?:银行)?(?:代扣|扣款)(?:授权|查询卡))|[，,](?=(?:银行)?查询卡)'
    position, reset = 0, True
    for separator in re.finditer(expression, text):
        yield text[position:separator.start()], reset
        position = separator.end()
        reset = bool(re.search(r'[。！？\n]', separator[0]))
    yield text[position:], reset


def execution_span_supported(clause, span, operations, target, reference_operations=None):
    """Check each remaining fact against its own local kind and explicit ID."""
    before = clause[:span['start']]
    start = max((before.rfind(separator) for separator in '，,：:；;。！？\n'), default=-1) + 1
    local = clause[start:span['end']]
    kind = operation_kind(span['matched_text']) or operation_kind(local) or (target[0] if target else None)
    if reference_operations is None:
        reference_operations = operations
    ids, unresolved = resolve_operation_references(local, reference_operations)
    if unresolved:
        return False
    operations = claim_operation_entities(local, operations, reference_operations)
    candidates = [operation for operation in operations.values()
                  if (not kind or operation['kind'] == kind)
                  and (not ids or operation['operation_id'] in ids)]
    if target:
        candidates = [operation for operation in candidates if operation['kind'] != target[0]
                      or operation['operation_id'] == target[1] or operation['operation_id'] in ids]
    return any(operation.get('status') == 'SUCCEEDED' and (operation.get('receipt') or operation.get('receipt_id'))
               for operation in candidates)


def negated_preparation_candidates(text):
    """Return (span, assertion class); uncertainty is not a prepared card."""
    expression = re.compile(
        r'未[ \t]*(?P<action>建立|生成|准备)[ \t]*(?:任何[ \t]*)?'
        r'(?P<object>(?:(?:银行|转账|撤销|取消代扣|代扣查询|代扣|查询)[ \t]*)?'
        r'(?:待[ \t]*确认[ \t]*(?:操作|卡(?:片)?)|确认卡(?:片)?|操作|请求))'
        r'(?=$|[ \t，,。；;：:！？!?\n）)])')
    nonassertion = re.compile(
        r'(?:不代表|不能说|没有说|不是说|不等于|无法断言|不能认定|'
        r'不能证明|没有证明|并不意味着|如果|假设|若)[ \t]*$')
    double_negative = re.compile(r'(?:不是|并非|并不是|不|未|没有|并未|尚未|还没|未能)[ \t]*$')
    candidates = []
    for match in expression.finditer(text):
        # A preceding independent clause does not negate this predicate.
        prefix = re.split(r'[，,。；;：:！？!?\n]', text[:match.start()])[-1]
        span = {'start': match.start(), 'end': match.end(), 'text': match[0],
                'action': match['action'], 'object': match['object'],
                'before': None, 'kind': operation_kind(match[0])}
        uncertainty = nonassertion.search(prefix)
        if uncertainty:
            # Remove only the local qualifier with its predicate. Leaving it
            # behind would let legacy NON_ASSERTION swallow a later success.
            span['start'] -= len(prefix) - uncertainty.start()
            span['text'] = text[span['start']:span['end']]
            classification = 'NONASSERTION'
        else:
            classification = 'DOUBLE_NEGATION' if double_negative.search(prefix) else 'DENIED'
        candidates.append((span, classification))
    return candidates


def preparation_refusal_spans(text):
    """Classify bounded refusals before preparation, not whole denied clauses."""
    expression = re.compile(
        r'在[ \t]*(?P<action>建立|生成|创建|准备)[ \t]*'
        r'(?P<object>(?:(?:银行|转账|撤销|取消代扣|代扣查询|代扣|查询)[ \t]*)?'
        r'(?:待[ \t]*确认[ \t]*(?:操作|卡(?:片)?)|确认卡(?:片)?|操作|请求))[ \t]*'
        r'(?P<before>之前|以前|前)[ \t]*(?:已经|已|就|即)?[ \t]*'
        r'(?:被[ \t]*)?(?:银行[ \t]*)?拒绝(?:了)?'
        r'(?=$|[ \t，,。；;：:！？!?\n）)])')
    spans = []
    for match in expression.finditer(text):
        prefix = text[max(0, match.start()-8):match.start()]
        if re.search(r'(?:不|未|没有|并非|不是|假设|如果|若)[ \t]*$', prefix):
            continue
        spans.append({'start': match.start(), 'end': match.end(), 'text': match[0],
                      'action': match['action'], 'object': match['object'],
                      'before': match['before'], 'kind': operation_kind(match[0])})
    spans.extend(span for span, classification in negated_preparation_candidates(text)
                 if classification != 'DOUBLE_NEGATION')
    return sorted(spans, key=lambda span: span['start'])


def preparation_claim_spans(text):
    """Require evidence for finite prepared objects, including double denial."""
    claims = [dict(span, classification=classification)
              for span, classification in negated_preparation_candidates(text)
              if classification == 'DOUBLE_NEGATION']
    expression = re.compile(
        r'已(?:经)?[ \t]*(?:建立|生成|准备)[ \t]*(?:任何[ \t]*)?'
        r'(?:(?:银行|转账|撤销|取消代扣|代扣查询|代扣|查询)[ \t]*)?'
        r'(?:待[ \t]*确认[ \t]*(?:操作|卡(?:片)?)|确认卡(?:片)?|操作|请求)'
        r'(?=$|[ \t，,。；;：:！？!?\n）)]|但|且|而)')
    for match in expression.finditer(text):
        before = re.split(r'[，,。；;：:！？!?\n]|但是|但|并且|且|而', text[:match.start()])[-1]
        if re.search(r'是否|(?:不|未|没有|不能|无法|若|如果|假设|将会|我会|之后|以后|后再)[ \t]*$', before):
            continue
        claims.append({'start': match.start(), 'end': match.end(), 'text': match[0],
                       'kind': operation_kind(match[0]), 'classification': 'ASSERTED'})
    return sorted(claims, key=lambda span: span['start'])


def preparation_claim_supported(clause, span, operations, target, refusal):
    """Match the local card's kind, identity and state, not another old card."""
    before = clause[:span['start']]
    separators = list(re.finditer(r'[，,：:；;。！？\n]|但是|但|并且|且|而', before))
    start = separators[-1].end() if separators else 0
    following = re.search(r'[，,：:；;。！？\n]|但是|但|并且|且|而', clause[span['end']:])
    end = span['end'] + following.start() if following else len(clause)
    local = clause[start:end]
    kind = span['kind'] or operation_kind(local) or (target[0] if target else None)
    ids, unresolved = resolve_operation_references(local, operations)
    if unresolved or any(identifier not in operations for identifier in ids):
        return False
    if refusal is not None and not ids and (refusal['kind'] is None or kind is None or kind == refusal['kind']):
        return False
    candidates = [op for op in operations.values()
                  if (not kind or op.get('kind') == kind)
                  and (not ids or op.get('operation_id') in ids)]
    if target:
        candidates = [op for op in candidates if op.get('kind') != target[0]
                      or op.get('operation_id') == target[1] or op.get('operation_id') in ids]
    return any(op.get('status') == 'PENDING' or
               (op.get('status') == 'SUCCEEDED' and (op.get('receipt') or op.get('receipt_id')))
               for op in candidates)


def mask_preparation_refusals(text):
    """Remove only non-asserting spans; retain their bank-kind context."""
    labels = {'transfer': '转账操作', 'mandate_query': '代扣查询操作',
              'mandate_cancel': '撤销操作', 'arrangement': '安排操作'}
    for span in reversed(preparation_refusal_spans(text)):
        label = labels.get(span['kind'], '银行操作')
        replacement = label.ljust(span['end'] - span['start'])
        text = text[:span['start']] + replacement + text[span['end']:]
    return text


def financial_arrangement_review_object(text):
    """Recognize a complete bounded list of arrangement information objects."""
    field = r'(?:到账条件|到账时间|日期|时间|金额|信息|明细)'
    modifier = r'(?:(?:固定|定期|每月|每周|本期|近期|未来|已记录)的?|已确认的)?'
    arrangement = modifier + r'(?:还款|收入|支出|收支)安排(?:的?' + field + r')?'
    financial_field = (r'(?:(?:项目|预计|已记录)的?|已确认的)?'
                       r'(?:收入|支出|还款)的?' + field)
    item = r'(?:' + arrangement + r'|' + financial_field + r'|' + field + r')'
    expression = (r'[ \t]*(?:(?:当前|现有|相关|对应|这些|该项)[ \t]*)?'
                  + arrangement + r'(?:[ \t]*(?:以及|与|和|及|、)[ \t]*'
                  + item + r'){0,2}[ \t]*')
    return re.fullmatch(expression, text) is not None


def confirmation_request_spans(clause):
    """Classify local review objects without suppressing other action claims."""
    operation_context = bool(operation_references(clause) or POSITIVE_PREPARE.search(clause)
        or re.search(r'操作|确认卡|卡片|页面|面板|弹窗|按钮|点击|点选|待[ \t]*确认', clause))
    information_object = re.compile(
        r'[ \t]*(?:(?:当前|现有|相关|对应|已记录的?|这些|这份|该项)[ \t]*)?'
        r'(?:安排|日期|到账条件|到账时间|余额|账单|流水|资金安排|收支明细|'
        r'(?:收入|支出|还款|转账|账户)(?:到账条件|到账时间|日期|时间|金额|信息))'
        r'(?=$|[ \t，,。；;：:！？!?\n、与和及的]|日期|时间|金额|信息|条件|明细)')
    spans = []
    # With multiple predicates, keep the legacy confirmation checks intact:
    # a later request can lie beyond its bounded invitation match.
    single_predicate = len(re.findall(r'确认|核对', clause)) == 1
    # The legacy bounded wildcard can span two comma-separated invitations.
    # Classify each local request so a later information object cannot replace
    # an earlier request to confirm a bank operation.
    for segment in re.finditer(r'[^，,；;。！？\n]+', clause):
        for match in WAIT_CONFIRM.finditer(segment[0]):
            # One bounded legacy match can still contain multiple predicates
            # without punctuation (confirm, then review). Preserve each one.
            start = segment.start()+match.start()
            for predicate in re.finditer(r'确认|核对', match[0]):
                verb = predicate[0]
                end = segment.start()+match.start()+predicate.end()
                # This predicate names a time before confirmation, not a request
                # to confirm now. Never consume another predicate or punctuation.
                prefix = clause[segment.start():end-len(verb)]
                suffix = clause[end:segment.end()]
                temporal = (single_predicate and verb == '确认'
                    and re.search(r'请[ \t]*(?:您|你)?[ \t]*在[ \t]*(?:(?:您|转账|操作)[ \t]*)?$', prefix)
                    and re.match(r'[ \t]*(?:之)?前[ \t]*(?:(?:自行|先|务必|仔细)[ \t]*){0,2}(?:留意|检查)', suffix))
                complete_information = (verb == '核对' and not operation_context
                    and financial_arrangement_review_object(suffix))
                if temporal:
                    classification = 'TEMPORAL_REFERENCE'
                elif operation_context or verb == '确认':
                    classification = 'OPERATION_CONFIRMATION'
                elif (information_object.match(clause[end:segment.end()])
                        or complete_information):
                    classification = 'INFORMATION_REVIEW'
                else:
                    classification = 'UNCLASSIFIED_CONFIRMATION'
                spans.append({'start': start, 'end': end, 'text': clause[start:end],
                              'verb': verb, 'classification': classification})
                start = end
                if complete_information:
                    # Full noun-list grammar permits only the adjective 已确认的,
                    # not another request. Do not classify that adjective twice.
                    break
    return spans


def posted_report_facts(records):
    """Read direct successful report results, never descriptions or echoed args."""
    facts = []
    for record in records:
        envelope = record.get('response', {})
        if (record.get('kind') != 'response' or record.get('ok') is not True
                or record.get('tool') != 'billing_report' or not isinstance(envelope, dict)
                or envelope.get('scope') != 'bank' or envelope.get('currency') != 'CNY'
                or not isinstance(envelope.get('evidence_id'), str) or not envelope['evidence_id']):
            continue
        data = envelope.get('data')
        report = data.get('report') if isinstance(data, dict) else None
        if (not isinstance(report, dict) or report.get('report_contract_version') != 2
                or report.get('currency') != envelope['currency']):
            continue
        totals = report.get('totals')
        refs = totals.get('evidence_refs') if isinstance(totals, dict) else None
        if (not isinstance(totals, dict) or type(totals.get('count')) is not int
                or totals['count'] < 0 or type(totals.get('net_change_minor')) is not int
                or not isinstance(refs, list) or any(not isinstance(ref, str) or not ref for ref in refs)
                or (totals['count'] > 0 and not refs)
                or (totals['count'] == 0 and totals['net_change_minor'] != 0)):
            continue
        try:
            start, end, as_of = (date.fromisoformat(value) for value in (
                report.get('start_date'), report.get('end_date'), envelope.get('as_of')))
        except (TypeError, ValueError):
            continue
        if not start <= end <= as_of:
            continue
        facts.append({'start_date': start.isoformat(), 'end_date': end.isoformat(),
                      'currency': envelope['currency'], 'net_change_minor': totals['net_change_minor'],
                      'evidence_id': envelope['evidence_id'], 'evidence_refs': list(refs)})
    return facts


def historical_posting_spans(clause, facts, period_dates=None):
    """License only the posted-funds attribute of one evidenced period change."""
    expression = re.compile(
        r'(?:本期|该期|该期间|本观察期间|该观察期间)(?P<posted>已入账)资金的净'
        r'(?P<direction>增加|减少|变化)(?=$|[\s，,。；;：:！？!?）)]|并且)')
    candidates = list(expression.finditer(clause))
    if not candidates:
        return []
    # Multiple periods/values are ambiguous even when a matching number exists.
    identities = {(fact['start_date'], fact['end_date'], fact['currency'], fact['net_change_minor'])
                  for fact in facts}
    identity = next(iter(identities)) if len(identities) == 1 else None
    amounts = {number(match['amount']) for match in re.finditer(
        r'净流入\s*(?:为|是|：|:)?\s*(?P<amount>[-+−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*元', clause)}
    dates = re.findall(r'\d{4}-\d{2}-\d{2}', clause) if period_dates is None else period_dates
    common = bool(identity and not operation_references(clause)
                  and amounts == {Decimal(identity[3]) / 100}
                  and (not dates or dates == [identity[0], identity[1]]))
    return [{'start': match.start('posted'), 'end': match.end('posted'),
             'text': match[0], 'supported': common and (
                 match['direction'] == '变化'
                 or (match['direction'] == '减少' and identity[3] < 0)
                 or (match['direction'] == '增加' and identity[3] > 0))}
            for match in candidates]


def receipt_read_operations(records):
    """Collect returned successful receipts from operation results, not echoes."""
    tools = {'operation', 'prepare_transfer', 'prepare_mandate_query',
             'prepare_mandate_cancel', 'propose_arrangement'}
    operations = {}

    def add(envelope):
        if not isinstance(envelope, dict) or envelope.get('scope', 'bank') != 'bank':
            return
        data = envelope.get('data')
        operation = data.get('operation') if isinstance(data, dict) else None
        if not isinstance(operation, dict):
            return
        identifier = operation.get('operation_id')
        if not isinstance(identifier, str) or not OP_ID.fullmatch(identifier):
            return
        # A later returned non-success cannot inherit an older success by ID.
        operations.pop(identifier, None)
        if (operation.get('kind') not in {'transfer', 'mandate_query', 'mandate_cancel', 'arrangement'}
                or operation.get('status') != 'SUCCEEDED'):
            return
        receipt = operation.get('receipt')
        if receipt is not None:
            if not isinstance(receipt, dict):
                return
            receipt_id = receipt.get('receipt_id')
            for field in ('operation_id', 'kind', 'status'):
                if field in receipt and receipt[field] != operation[field]:
                    return
        else:
            receipt_id = operation.get('receipt_id')
        if isinstance(receipt_id, str) and receipt_id:
            operations[identifier] = operation

    for record in successful_responses(records):
        envelope = record['response']
        if not isinstance(record.get('tool'), str):
            continue
        if record.get('tool') in tools:
            add(envelope)
        elif record.get('tool') in {'task_status', 'submit_task_plan', 'replan_task'}:
            data = envelope.get('data')
            task = data.get('task') if isinstance(data, dict) else None
            if (not isinstance(task, dict) or task.get('scope') != 'bank'
                    or task.get('subject_id') != 'U1' or not task.get('task_id')
                    or not isinstance(task.get('nodes'), list)):
                continue
            for node in task['nodes']:
                if (isinstance(node, dict) and node.get('state') == 'SUCCEEDED'
                        and isinstance(node.get('tool'), str) and node['tool'] in tools):
                    add(node.get('result'))
    return operations


def action_reasons(text, operations, tasks, status_tasks=None, reference_operations=None, posted_reports=None,
                   receipt_operations=None):
    if reference_operations is None:
        reference_operations = operations
    if receipt_operations is None:
        receipt_operations = operations
    text = remove_conditional_reconcile_advice(text.replace('*', '').replace('`', ''))
    scoped_reasons = []

    def validate_future_suffix(remainder, binding):
        # Same-kind old receipts cannot prove the target future operation's state.
        # An uncreated prepare has no usable operation ID. Other explicit kinds
        # still use their own evidence, as in "查询卡已准备，请确认".
        for clause in re.split(r'[。！？；\n]', remainder):
            explicit_ids, _ = resolve_operation_references(clause, reference_operations)
            claim_operations = claim_operation_entities(clause, operations, reference_operations)
            scoped_operations = {key: operation for key, operation in claim_operations.items()
                                 if operation.get('kind') != binding['kind']
                                 or key == binding['operation_id'] or key in explicit_ids}
            scoped_reasons.extend(action_reasons(clause, scoped_operations, tasks,
                reference_operations=reference_operations,
                receipt_operations={key: value for key, value in receipt_operations.items()
                                    if key in scoped_operations}))

    text = remove_verified_future_plan_steps(text, status_tasks or {}, suffix_validator=validate_future_suffix)
    text, reasons = remove_verified_task_summary(text.replace('*', '').replace('`', ''), status_tasks or {})
    text, node_reasons = remove_verified_readonly_node_success(text, status_tasks or {})
    reasons.extend(node_reasons)
    reasons.extend(scoped_reasons)
    # Keep comma-separated subject and predicate together; reset at full stops
    # and newlines. This is deliberately a finite Chinese assertion detector.
    text = re.sub(r'「[^」]*」|“[^”]*”|"[^"\n]*"', quoted_action_label, text)
    inherited_target = None
    stage_context = False
    stage_task_id = None
    refusal_binding = None
    period_dates = []
    for raw, reset in action_clause_parts(text):
        if reset:
            inherited_target = None
            stage_context = False
            stage_task_id = None
            refusal_binding = None
            period_dates = []
        clause = raw.replace('*', '').strip()
        progress = task_stage_progress_spans(clause, status_tasks or {}, text)
        if len(progress) > 1:
            # Do not guess which of two simultaneous stages an anonymous tail
            # refers to. Preserve the original text and fail the ambiguity.
            reasons.append({'code': 'UNBACKED_TASK_STAGE_PROGRESS', 'clause': clause,
                            'evidence_context': 'MULTIPLE_STAGE_PREDICATES'})
            progress = []
        if progress:
            stage = progress[0]
            stage_context, stage_task_id = True, stage['task_id']
            if not stage['supported']:
                reasons.append({'code': 'UNBACKED_TASK_STAGE_PROGRESS',
                                'clause': clause[stage['start']:stage['end']]})
            inherited_target = (stage['kind'], stage['operation_id'])
            before, tail = clause[:stage['start']].strip(), clause[stage['end']:].strip()
            readonly_tail = (stage_readonly_completion(tail[1:], (status_tasks or {}).get(stage_task_id))
                             if tail.startswith(('，', ',')) else None)
            if not before and (not tail or readonly_tail is not None):
                if readonly_tail is False:
                    reasons.append({'code': 'UNBACKED_READONLY_NODE_SUCCESS', 'clause': tail[1:]})
                # Both independent predicates now have their own typed proof.
                clause = ' ' * len(clause)
            elif not tail.startswith(('，', ',')):
                # Unknown conjunctions retain all original action checks; they
                # are not a new grammar exemption for a mixed statement.
                reasons.append({'code': 'UNBACKED_TASK_STAGE_PROGRESS', 'clause': clause,
                                'evidence_context': 'NONINDEPENDENT_STAGE_PREDICATE'})
        if stage_context:
            readonly = stage_readonly_completion(clause, (status_tasks or {}).get(stage_task_id))
            if readonly is not None:
                if not readonly:
                    reasons.append({'code': 'UNBACKED_READONLY_NODE_SUCCESS', 'clause': clause})
                clause = ' ' * len(clause)
        period_dates.extend(re.findall(r'\d{4}-\d{2}-\d{2}', clause))
        historical = historical_posting_spans(clause, posted_reports or [], period_dates)
        for span in reversed(historical):
            if span['supported']:
                # Mask only the historical attribute; all other verbs/IDs stay.
                clause = clause[:span['start']] + ' ' * (span['end'] - span['start']) + clause[span['end']:]
            else:
                reasons.append({'code': 'UNBACKED_OPERATION_SUCCESS', 'clause': clause,
                                'evidence_context': 'UNSUPPORTED_HISTORICAL_POSTING'})
        double_denial = any(classification == 'DOUBLE_NEGATION'
                           for _, classification in negated_preparation_candidates(clause))
        refusals = preparation_refusal_spans(clause)
        if refusals:
            # A refusal before creation supplies no operation identity. Keep
            # its local subject, so an anonymous neighbour cannot borrow an
            # unrelated old receipt. Explicit other IDs remain independently
            # checkable; a full stop or newline resets this bounded context.
            kinds = {span['kind'] or operation_kind(clause[:span['start']]) for span in refusals}
            refusal_binding = {'kind': next(iter(kinds)) if len(kinds) == 1 else None}
            clause = mask_preparation_refusals(clause)
        if explains_receipt_boundary(clause):
            continue
        # Strip the bounded non-proof span before the legacy non-assertion test;
        # otherwise its "不能" could hide a neighbouring actual bank success.
        clause = asserted_action_text(clause)
        spans = execution_claim_spans(clause)
        if stage_context:
            # Only an anonymous completion in this new target scope. Named
            # read-only objects (before or after the verb) remain separate.
            for match in re.finditer(r'已(?:经)?完成(?:了)?(?:转账|代扣查询|代扣撤销)?(?=[ \t]*(?:[，,；;。！？!?：:\n]|$))', clause):
                before = clause[:match.start()]
                local = re.split(r'[，,；;：:。！？!?\n]', before)[-1].strip()
                if (re.fullmatch(r'(?:(?:本次|这次|该笔|这笔|当前|银行|转账|查询|撤销|取消|操作|op_[A-Za-z0-9]+)|[ \t])*', local)
                        and not any(span['start'] <= match.start() < span['end'] for span in spans)):
                    spans.append({'start': match.start(), 'end': match.end(), 'matched_text': match[0],
                                  'classification': 'ASSERTED', 'condition_end': None, 'action': None,
                                  'reason': 'ANONYMOUS_STAGE_COMPLETION'})
            spans.sort(key=lambda span: span['start'])
            if (re.match(r'已', clause.lstrip()) and not spans
                    and not POSITIVE_PREPARE.search(clause)
                    and not confirmation_request_spans(clause)):
                # Removing a valid stage must not approve an unparsed present
                # status in its local continuation. No global synonym expansion.
                reasons.append({'code': 'UNBACKED_TASK_STAGE_PROGRESS', 'clause': clause,
                                'evidence_context': 'UNCOVERED_STAGE_STATUS'})
        bindings = []
        for span in spans:
            if span['classification'] != 'TEMPORAL_CONDITION':
                continue
            binding = temporal_condition_binding(span, status_tasks or {}, text)
            if binding:
                bindings.append(binding)
            else:
                # Keep the existing decision boundary when no actual task proves
                # this receipt workflow. The pure classifier still reports time.
                span['classification'] = 'ASSERTED'
        has_condition = any(span['classification'] == 'TEMPORAL_CONDITION' for span in spans)
        targets = {(binding['kind'], binding['operation_id']) for binding in bindings}
        if len(targets) == 1:
            inherited_target = next(iter(targets))
        elif targets:
            inherited_target = None
        # These finite card claims must be checked before a neighbouring denial
        # can trigger the legacy whole-clause NON_ASSERTION early return.
        for claim in preparation_claim_spans(clause):
            preparation_sources = {key: value for key, value in reference_operations.items()
                                   if key in operations}
            if not preparation_claim_supported(clause, claim, preparation_sources,
                                               inherited_target, refusal_binding):
                reasons.append({'code': 'UNBACKED_OPERATION_PREPARATION', 'clause': clause,
                                'evidence_context': 'UNSUPPORTED_PREPARED_OBJECT'})
        local_execution_check = has_condition or bool(historical) or stage_context
        actual_spans = actual_execution_spans(clause, spans) if local_execution_check else None
        # Reading a receipt is an explicit fact even without a bank-kind noun.
        # Check each local assertion before whole-clause non-assertion exits.
        for span in actual_execution_spans(clause, spans):
            if not re.fullmatch(r'已(?:读取|核对)(?:真实|银行)?回执', span['matched_text']):
                continue
            sources = receipt_operations
            ids, _ = resolve_operation_references(clause, reference_operations)
            kind = operation_kind(clause)
            if (refusal_binding is not None and not ids
                    and (refusal_binding['kind'] is None or kind is None or kind == refusal_binding['kind'])):
                sources = {}
            if not execution_span_supported(clause, span, sources, inherited_target, sources):
                reasons.append({'code': 'UNBACKED_OPERATION_SUCCESS', 'clause': clause,
                                'evidence_context': 'UNSUPPORTED_RECEIPT_READ'})
        nonassertion_text = execution_context_without_conditions(clause, spans)
        nonassertion = NON_ASSERTION.search(nonassertion_text)
        # A local time condition/denial may explain one match, never a later
        # independent positive assertion in this same punctuation clause.
        if not clause or (nonassertion and not double_denial and not (local_execution_check and actual_spans)) or requests_details_before_future_preparation(clause) or explains_verification_standard(clause) or offers_optional_future_service(clause):
            continue
        read_complete = readonly_completion(clause, tasks)
        if read_complete is not None:
            if not read_complete:
                reasons.append({'code': 'UNBACKED_ANALYSIS_COMPLETION', 'clause': clause})
            continue
        if TASK_COMPLETE.search(clause):
            ids = re.findall(r'\b[a-f0-9]{32}\b', clause)
            matching_tasks = [t for key, t in tasks.items() if not ids or key in ids]
            if not any(t['state'] == 'SUCCEEDED' and t['nodes'] and all(n.get('state') == 'SUCCEEDED' for n in t['nodes']) for t in matching_tasks):
                reasons.append({'code': 'UNBACKED_TASK_COMPLETION', 'clause': clause})
        if TASK_SAVED.search(clause) and not tasks:
            reasons.append({'code': 'UNBACKED_TASK_SAVE', 'clause': clause})
        ids, unresolved = resolve_operation_references(clause, reference_operations)
        # The new stage scope must not turn legacy recursively collected args
        # into proof for a neighbouring bank assertion.
        operation_sources = ({key: value for key, value in reference_operations.items() if key in operations}
                             if stage_context else operations)
        claim_operations = claim_operation_entities(clause, operation_sources, reference_operations)
        explicit_kind = operation_kind(clause)
        kind = explicit_kind
        if not kind and inherited_target and (POSITIVE_EXECUTE.search(clause) or POSITIVE_PREPARE.search(clause) or WAIT_CONFIRM.search(clause) or (stage_context and actual_spans)):
            kind = inherited_target[0]
        candidates = [op for op in claim_operations.values() if not unresolved
                      and (not kind or op['kind'] == kind) and (not ids or op['operation_id'] in ids)]
        if inherited_target:
            target_kind, target_id = inherited_target
            candidates = [op for op in candidates if op['kind'] != target_kind
                          or op['operation_id'] == target_id or op['operation_id'] in ids]
        action_context = bool(kind or ids or re.search(r'操作|确认卡|确认弹窗|银行面板|查询待.{0,3}确认', clause)
                              or (operations and POSITIVE_EXECUTE.search(clause))
                              or refusal_binding is not None or bool(historical))
        if not action_context:
            continue
        prepared = bool(POSITIVE_PREPARE.search(clause))
        waiting = any(span['classification'] not in {'INFORMATION_REVIEW', 'TEMPORAL_REFERENCE'}
                      for span in confirmation_request_spans(clause)) and not re.search(
                          r'是否|还是|多少|具体金额|哪(?:一)?天|哪(?:个|种)', clause)
        # Each assertion is checked separately: a nearby question does not
        # suppress another genuine success assertion in the same clause.
        executed = bool(actual_spans) if local_execution_check else any(
            not re.search(r'是否\s*$', clause[max(0, match.start()-8):match.start()])
            for match in POSITIVE_EXECUTE.finditer(clause))
        refusal_without_new_identity = (refusal_binding is not None and not ids
            and (refusal_binding['kind'] is None or explicit_kind is None
                 or explicit_kind == refusal_binding['kind']))
        if refusal_without_new_identity:
            candidates = []
        if (prepared or waiting) and not any(op.get('status') in {'PENDING', 'SUCCEEDED'} for op in candidates):
            reasons.append({'code': 'UNBACKED_OPERATION_PREPARATION', 'clause': clause})
        if waiting and candidates and not any(op.get('status') == 'PENDING' for op in candidates):
            reasons.append({'code': 'OPERATION_NOT_PENDING', 'clause': clause})
        execution_supported = (not unresolved and all(execution_span_supported(clause, span, operation_sources, inherited_target, reference_operations)
                                   for span in actual_spans) if local_execution_check else
                               any(op.get('status') == 'SUCCEEDED' and (op.get('receipt') or op.get('receipt_id')) for op in candidates))
        if refusal_without_new_identity:
            execution_supported = False
        if executed and not execution_supported:
            reasons.append({'code': 'UNBACKED_OPERATION_SUCCESS', 'clause': clause})
    return reasons


def validate_bank_answer(result, prompt):
    """Return a new result; rejected raw text stays diagnostics, never normal UI."""
    if not result.get('ok') or not result.get('text'):
        return {**result, 'answer_validation': {'version': CONTRACT_VERSION, 'status': 'MODEL_FAILED', 'gate_applied': True}}
    from src.bank_evidence.envelope import normalize
    try:
        result = normalize(result)
    except (ValueError, TypeError) as error:
        return {**result, 'ok': False, 'model_ok': True, 'session_id': None,
                'error': '回答格式无法完整识别，未作为核验结果展示；请查看已有银行记录。',
                'answer_validation': {'version': 'answer-envelope-1', 'scope': 'answer_envelope',
                                      'status': 'REJECTED', 'gate_applied': True, 'reason': str(error)}}
    from src.bank_evidence.calculation import provenance_reasons
    input_reasons = provenance_reasons(successful_responses(result.get('trace', [])), prompt)
    if input_reasons:
        return {**result, 'ok': False, 'model_ok': True, 'session_id': None,
                'error': '计算中的拟议金额未能对应您的原话，请重新说明金额。银行记录未因此改变。',
                'answer_validation': {'version': CONTRACT_VERSION, 'scope': 'calculation_sources',
                                      'status': 'REJECTED', 'gate_applied': True, 'reasons': input_reasons}}
    from src.bank_goals.impact_contract import requirement
    from src.bank_goals.impact_answer import validate as validate_impact
    try:
        document = json.loads(result['text'])
    except (ValueError, TypeError):
        document = {}
    if isinstance(document, dict) and document.get('format') == 'goal_impact_v1':
        return validate_impact(result, prompt)
    from src.bank_requests.components import records as impact_records
    for row in impact_records(result.get('trace', [])):
        body = row.get('response') or {}; data = body.get('data', {})
        target = data.get('target') or ({'profile': data['profile'],
            'arrangement': (data.get('current_plan') or {}).get('arrangement')} if data.get('profile') else None)
        if row.get('ok') and requirement(prompt, target):
            return {**result, 'ok': False, 'error': '这次问题需要完整的目标影响比较，请调用对应的只读工具。',
                    'answer_validation': {'status': 'REJECTED', 'reason': 'GOAL_IMPACT_RELATION_NOT_COVERED'}}
    from src.bank_evidence.service_answer import requires_outcomes, requires_insights, validate as validate_service_outcomes
    if requires_outcomes(result) or requires_insights(result):
        return validate_service_outcomes(result, prompt)
    from .task_progress_answer import requested as progress_requested, validate as validate_progress
    if progress_requested(result):
        return validate_progress(result)
    from src.bank_goals.evidence import has_goal_evidence, validate_answer
    from src.bank_goals.preview_answer import has_preview, validate as validate_preview
    if has_preview(result):
        return validate_preview(result)
    from src.bank_goals.clarification import missing_goal_clarification, clarification_reasons
    goal_clarification = missing_goal_clarification(result)
    if has_goal_evidence(result) and goal_clarification is None:
        return validate_answer(result)
    from src.bank_evidence.service_answer import requested as service_requested, required as service_required, validate as validate_service
    if service_requested(result) or service_required(result):
        return validate_service(result, prompt)
    from src.bank_evidence.spending_answer import requested as spending_requested, validate as validate_spending
    if spending_requested(result):
        return validate_spending(result, prompt)
    from src.bank_evidence.billing_answer import required as billing_required, requested as billing_requested, validate as validate_billing
    if billing_requested(result) or billing_required(result):
        return validate_billing(result, prompt)
    from src.bank_evidence.answer import required as facts_required, requested as facts_requested, validate as validate_facts
    if facts_required(result) or facts_requested(result):
        return validate_facts(result, prompt)
    amounts, percentages = {Decimal(0)}, set()
    operations, tasks, negative_balances = {}, {}, set()
    negative_percentages, policy_amounts, policy_percentages = set(), set(), set()
    records = successful_responses(result.get('trace', []))
    for record in records:
        values = record['response'].get('data', {})
        if record.get('tool') == 'money_calculate':
            # Echoed operands are not newly queried financial facts.
            values = values.get('calculation', {}).get('result', {})
        collect_values(values, amounts, percentages, negative_balances, negative_percentages, policy_amounts, policy_percentages,
                       calculation=record.get('tool') == 'money_calculate')
        collect_entities(record['response'].get('data', {}), operations, tasks)
    # Proposed user amounts can be repeated, but do not authorize a new result.
    cny_amounts, cny_deficits, cny_policies = cny_amount_values(result.get('trace', []))
    for mention in amount_mentions(prompt):
        if mention['status'] != 'VALID': continue
        value = number(mention['match'].group())
        amounts.add(value)
        if mention['origin'] == 'RMB_PREFIX' or yuan_assumption(prompt, mention): cny_amounts.add(value)
    percentages.update(number(m.group()) for m in PERCENT.finditer(prompt))
    percentages.update(value for _, value in chinese_percentages(prompt))
    unsupported = set()
    for mention in amount_mentions(result['text']):
        if mention['status'] == 'NOT_AMOUNT': continue
        if mention['status'] == 'INVALID_FORMAT':
            unsupported.add(mention['raw']); continue
        match = mention['match']
        explicit = mention['origin'] == 'RMB_PREFIX'
        permitted, deficits, policies = ((cny_amounts, cny_deficits, cny_policies) if explicit else
                                        (amounts, negative_balances, policy_amounts))
        view = currency_semantic_text(result['text'], mention)
        if (number(match.group()) not in permitted and not deficit_display(view, match, deficits)
                and not policy_display(view, match, policies)):
            unsupported.add(mention['raw'] if explicit else match.group())
    unsupported = sorted(unsupported)
    ratio_display_validation = billing_ratio_display_checks(result['text'], result.get('trace', []))
    percent_text = ratio_display_validation.pop('numeric_text')
    unsupported_percent = sorted({m.group() for m in PERCENT.finditer(percent_text) if number(m.group()) not in percentages
                                  and not decline_display(percent_text, m, negative_percentages)
                                  and not policy_display(percent_text, m, policy_percentages)})
    unsupported_percent = sorted(set(unsupported_percent) | {token
        for token, value in chinese_percentages(percent_text) if value not in percentages})
    reasons = action_reasons(result['text'], operations, tasks, current_status_tasks(result.get('trace', [])),
                             reference_operations=visible_reference_operations(records),
                             posted_reports=posted_report_facts(records),
                             receipt_operations=receipt_read_operations(records))
    if goal_clarification is not None:
        reasons.extend(clarification_reasons(result['text'], result.get('trace', [])))
    minimum_validation = forecast_minimum_checks(result['text'], result.get('trace', []))
    reasons.extend(minimum_validation['reasons'])
    billing_period_validation = billing_previous_period_checks(result['text'], result.get('trace', []))
    reasons.extend(billing_period_validation['reasons'])
    numeric_diagnostics = list(minimum_validation['reasons'])
    if unsupported:
        missing_amount = {'code': 'UNSUPPORTED_DISPLAYED_AMOUNT', 'values': unsupported}
        numeric_diagnostics.append(missing_amount)
        reasons.append(missing_amount)
    if unsupported_percent:
        missing_percent = {'code': 'UNSUPPORTED_DISPLAYED_PERCENT', 'values': unsupported_percent}
        numeric_diagnostics.append(missing_percent)
        reasons.append(missing_percent)
    validation = {'version': CONTRACT_VERSION, 'gate_applied': True, 'status': 'REJECTED' if reasons else 'ACCEPTED',
                  'reasons': reasons, 'response_count': len(records), 'operation_ids': list(operations), 'task_ids': list(tasks),
                  'forecast_minimum_checks': minimum_validation['checks'],
                  'billing_period_checks': billing_period_validation,
                  'billing_ratio_display_checks': ratio_display_validation,
                  'goal_clarification': goal_clarification,
                  'numeric_diagnostics': numeric_diagnostics, 'numeric_policy': 'AMOUNT_AND_PERCENT_ENFORCED',
                  'scope': 'bank', 'limitation': '有限中文动作、阿拉伯数字金额/百分比及部分中文成数来源检查；成数接受实际比例或用户原始假设同值，另允许最新账单工具提供且完整匹配的带指标/期间标签与精确值的近似显示；不自行推导补数或任意近似值；政策值仅限既有阿拉伯规则语境。值匹配不证明同值字段含义、用户假设已发生、计算输入事实正确或当前状态仍未变化；不覆盖所有自然语言金额写法。明确CNY/RMB/人民币前缀使用独立人民币来源与有限格式检查；裸币符及一般外币写法不代表已完成币种核验。预测最低值仅对forecast_minimum_checks标为VERIFIED的有限单快照口径进行金额日期绑定，NOT_COVERED项不代表事实已核验。'}
    if not reasons:
        return {**result, 'answer_validation': validation}
    return {**result, 'ok': False, 'model_ok': True, 'session_id': None,
            'error': '这次回答中的操作状态、金额、日期或比例未能和工具依据对齐，未作为成功结果展示。已有操作请以银行面板和原回执为准。',
            'answer_validation': validation}
