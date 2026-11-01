"""Account requests cannot silently become an all-account billing report."""
import re
from .normalization import source_record
from .write_prohibitions import mask as mask_write_prohibitions


def validate_request(item, context, rows):
    if item['intent'] != 'billing':
        return
    message = next(m['text'] for m in context['messages'] if m['message_id'] == context['current_message_id'])
    # A complete prohibition on moving B's money does not request B's bill.
    # The original message remains intact for source and write-authority checks.
    message = mask_write_prohibitions(message)
    observations = [r for r in rows if r['tool'] == 'accounts' and r['ok'] is True and r['status'] == 200]
    named = set()
    if observations:
        last = observations[-1]
        body = source_record({'evidence_id': last['response']['evidence_id']}, rows, tool='accounts')
        named = {a['account_id'] for a in body['data']['accounts'] if a.get('name') and a['name'] in message}
    explicit = set(re.findall(r'(?:账户\s*((?>[A-Za-z0-9]+))(?![A-Za-z0-9_年月日号])|'
                              r'(?<![A-Za-z0-9_])([A-Za-z0-9]+)\s*账户)', message))
    identifiers = named | {value for pair in explicit for value in pair if value}
    slot = item['slots'].get('source_account')
    if len(identifiers) > 1:
        raise ValueError('AMBIGUOUS_BILLING_ACCOUNT_SCOPE')
    if identifiers and (slot is None or slot['value'] not in identifiers):
        raise ValueError('BILLING_ACCOUNT_SCOPE_OMITTED')
    # Unrecognized named cards must not fall through to the default total.
    if not identifiers and slot is None:
        unknown_card = re.search(r'[\u4e00-\u9fff]{1,8}(?:银行卡|卡)', message)
        unknown_account = re.search(r'[\u4e00-\u9fff]{1,8}账户', message)
        generic = re.search(r'(?:所有|全部|各|名下|我的|银行)账户', message)
        if unknown_card or unknown_account and not generic:
            raise ValueError('UNRESOLVED_BILLING_ACCOUNT_SCOPE')
