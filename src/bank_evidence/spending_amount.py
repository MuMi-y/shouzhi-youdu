"""Bounded omitted-yuan amounts for read-only CNY spending comparisons only."""
from decimal import Decimal
import re

NUMBER = r'(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d{1,2})?'
VERB = r'(?:花费|消费|支出|花)'
# Do not consume a numeric prefix of a model number, foreign unit, range or rate.
AMOUNT = NUMBER + r'\s*(?:元)?(?![\d.,A-Za-z_%％万千百美元港欧日英块角分]|(?:人民币|左右|上下|多|余|到|至|或))'
PHRASE = re.compile(VERB + r'\s*(?P<number>' + NUMBER + r')\s*(?P<unit>元)?'
                    r'(?![\d.,A-Za-z_%％万千百美元港欧日英块角分]|(?:人民币|左右|上下|多|余|到|至|或))')
UNRESOLVED = re.compile(r'不是|不要|别|不想|不打算|不准备|不花|不消费|不支出|取消|大约|大概|约|左右|差不多|或者|或|到\s*\d|至\s*\d|[-~～—]', re.I)
CURRENCY_CONTEXT = re.compile(r'美元|美金|美刀|港币|港元|欧元|日元|英镑|外币|USD|EUR|HKD|JPY|GBP|[$€£]', re.I)


def omitted_yuan_matches(quote, message, value_minor):
    """Keep semantic context from the full clause, never just a cropped numeral.

    A caller must separately establish a read-only spending contract. Explicit
    currency amounts continue through the original amount validator.
    """
    if not isinstance(message, str) or not isinstance(quote, str) or message.count(quote) != 1:
        return False
    from src.bank_requests.write_prohibitions import mask
    # A comma does not reset the currency or erase a second price alternative.
    # When currency is omitted, never select one monetary meaning across these.
    if CURRENCY_CONTEXT.search(message) or re.search(r'或者?\s*\d', mask(message)):
        return False
    begin = message.index(quote); end = begin + len(quote)
    matches = []
    for clause in re.finditer(r'[^，,。；;!?！？\n]+', message):
        if clause.start() >= end or clause.end() <= begin:
            continue
        text = clause.group()
        # An ISO date is not a monetary range; date validation handles it.
        checked = re.sub(r'\d{4}-\d{2}-\d{2}', '', mask(text))
        if UNRESOLVED.search(checked):
            return False
        if re.search(r'(?:和|及|加|另|再|、)\s*(?:再|加上)?\s*\d', checked):
            return False
        found = list(PHRASE.finditer(text))
        if len(found) != 1:
            return False
        match = found[0]
        start_num = clause.start() + match.start('number')
        end_num = clause.start() + match.end('number')
        if not (begin <= start_num < end_num <= end) or match['unit']:
            return False
        matches.append(Decimal(match['number'].replace(',', '')) * 100)
    return len(matches) == 1 and matches[0] == value_minor
