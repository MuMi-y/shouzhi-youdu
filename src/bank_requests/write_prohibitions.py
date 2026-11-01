"""Bounded write-prohibition clauses, preserving original source offsets.

This does not classify arbitrary negation. Only complete known write actions
may be masked by read-date checks; the original prohibition remains available
to the action/authority checks. An unrecognized continuation is never removed.
"""
import re

NEGATOR = r'(?:不要|不用|不必|不想|不需要|无需|别|不)'
ACTION = (r'(?:(?:创建|新建|保存|新增|修改|改变|改|更新|取消|删除)(?:任何|新的|新|这个|那个)?'
          r'(?:计划|目标|安排|草案|任务)|'
          r'(?:准备)?(?:转账|付款|扣款|支付)|'
          r'(?:动用|动|操作)(?:日常账户|储备账户|账户[AB]|[AB]账户|资金)(?:的?(?:资金|余额|钱))?|'
          r'(?:办理|执行)(?:(?:任何|其他|其它)?(?:操作|业务|交易|转账|付款|扣款|支付))?)')
BODY = NEGATOR + r'(?:再|自动|擅自|帮我|为我)?' + ACTION
BODY += r'(?:(?:或|和|与|及|以及|、)(?:' + NEGATOR + r')?(?:再|自动)?' + ACTION + r')*'
PROHIBITION = re.compile(r'(?:^|[，,。；;！!？?\n]|(?<=[日号])|但|但是|并且|而且|同时)'
                         r'\s*(?:请|请你|先)?(?P<body>' + BODY + r')'
                         r'(?=\s*(?:[，,。；;！!？?\n]|$|(?:只|仅)(?:做)?(?:查|看|分析|比较|计算)))')


def spans(message):
    return [match.span('body') for match in PROHIBITION.finditer(message)]


def mask(message):
    result = list(message)
    for start, end in spans(message):
        result[start:end] = ' ' * (end - start)
    return ''.join(result)
