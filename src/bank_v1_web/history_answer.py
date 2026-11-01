"""Check displayed historical amounts against actual tool evidence, without model arithmetic."""
from decimal import Decimal, InvalidOperation
import re

# Only two-decimal amounts/percentages. Dates, row counts, DOI identifiers and hashes are not amounts.
DISPLAY_NUMBER = re.compile(r'(?<![0-9.,])[-+−]?(?:\d{1,3}(?:,\d{3})+|\d+)\.\d{2}(?![\d.])')
PROMPT_NUMBER = re.compile(r'(?<![0-9.,])[-+−]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?(?![0-9.])')
PERCENT_NUMBER = re.compile(r'(?<![0-9.,])\d+(?:\.\d+)?(?=\s*[%％])')


def decimal_value(text):
    return abs(Decimal(text.replace(',', '').replace('−', '-')))


def evidence_values(value, *, percentages_only=False):
    """Source descriptions never authorize numbers; only typed amount/percentage fields do."""
    values = set()
    if isinstance(value, list):
        for item in value:
            values.update(evidence_values(item, percentages_only=percentages_only))
    elif isinstance(value, dict):
        for key, item in value.items():
            if key.endswith('_minor') and type(item) is int and not percentages_only:
                values.add(abs(Decimal(item) / Decimal(100)))
            elif key in {'credit_share_percent', 'debit_share_percent'} and isinstance(item, str):
                try:
                    values.add(decimal_value(item))
                except InvalidOperation:
                    pass
            elif isinstance(item, (dict, list)):
                values.update(evidence_values(item, percentages_only=percentages_only))
    return values


def validate_history_answer(result, prompt):
    """A narrow numeric guard, not a claim that all prose has been semantically verified."""
    if not result.get('ok') or not result.get('text'):
        return result
    known = {Decimal(0)}
    known_percent = set()
    for record in result.get('trace', []):
        if (isinstance(record, dict) and record.get('kind') == 'response'
                and record.get('ok') is True and record.get('tool', '').startswith('moneydata_')
                and isinstance(record.get('response'), dict)
                and record['response'].get('scope') == 'moneydata'):
            known.update(evidence_values(record['response'].get('data', {})))
            known_percent.update(evidence_values(record['response'].get('data', {}), percentages_only=True))
    # A quoted proposed amount from the user's question is not an invented ledger amount.
    known.update(decimal_value(match.group()) for match in PROMPT_NUMBER.finditer(prompt))
    known_percent.update(decimal_value(match.group()) for match in PERCENT_NUMBER.finditer(prompt))
    unsupported = sorted({match.group() for match in DISPLAY_NUMBER.finditer(result['text'])
                          if decimal_value(match.group()) not in known})
    unsupported.extend(match.group() + '%' for match in PERCENT_NUMBER.finditer(result['text'])
                       if decimal_value(match.group()) not in known_percent)
    if not unsupported:
        return {**result, 'amount_check': {'passed': True}}
    return {**result, 'ok': False,
            'error': '这次解释中的金额未能与工具依据对齐，未作为成功分析展示。历史资料没有改变；可查看上方确定性统计并重新查询。',
            'amount_check': {'passed': False, 'unsupported': unsupported}}
