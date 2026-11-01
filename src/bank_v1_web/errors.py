"""Translate provider failures without exposing implementation details to users."""

def failure_message(result):
    error = result.get('error')
    if isinstance(error, str) and error.strip():
        return error
    codes, messages = [], []
    def collect(value, depth=0):
        if not isinstance(value, dict) or depth > 6:
            return
        if isinstance(value.get('code'), str):
            codes.append(value['code'].upper())
        if isinstance(value.get('message'), str):
            messages.append(value['message'])
        for key in ('error', 'reason'):
            collect(value.get(key), depth + 1)
    collect(error)
    if any('TIMEOUT' in code for code in codes) or any('timed out' in text.lower() for text in messages):
        return '这次分析超时，没有获得完整回答。已入账收支仍然有效；请先查看待确认操作和回执，再重新查询。'
    if result.get('ok'):
        return ''
    return '这次分析未完成。已入账收支仍然有效；请查看原操作状态，可以重新查询，系统不会自动重复办理。'
