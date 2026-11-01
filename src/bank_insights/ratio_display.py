"""Optional display phrases, never inputs to monetary or policy decisions."""
from decimal import Context, Decimal, ROUND_HALF_EVEN, localcontext


def metric_label(name, totals_path):
    period = {'totals': '本期', 'previous_period.totals': '前期'}.get(totals_path)
    metric = {'expense_to_income': '消费支出', 'net_flow_to_income': '净流入'}.get(name)
    return f'{period}{metric}占收入' if period and metric else None


def ratio_display(numerator, denominator, *, label):
    """Round independently from the raw integer ratio, not a rounded percentage."""
    if type(numerator) is not int or type(denominator) is not int or denominator <= 0 or not label:
        return None
    with localcontext(Context(prec=40, rounding=ROUND_HALF_EVEN)):
        ratio = Decimal(numerator) * 100 / Decimal(denominator)
        exact = ratio.quantize(Decimal('0.01'))
        whole = ratio.quantize(Decimal('1'))
        exact_text = format(Decimal(0) if exact == 0 else exact, '.2f') + '%'
        rounded = format(Decimal(0) if whole == 0 else whole, '.0f')
    phrases = [f'{label}约{rounded}%（{exact_text}）']
    # Only common, unambiguous 成数. Negative and >100% ratios use Arabic digits.
    if 10 <= whole <= 100:
        tens, ones = divmod(int(whole), 10)
        digits = '零一二三四五六七八九十'
        cheng = digits[tens] + '成' + (digits[ones] if ones else '')
        phrases.append(f'{label}约{cheng}（{exact_text}）')
    return {'version': 1, 'exact_text': exact_text, 'rounded_value': rounded,
            'quantum': '1', 'unit': 'percent', 'rounding': 'ROUND_HALF_EVEN', 'phrases': phrases}
