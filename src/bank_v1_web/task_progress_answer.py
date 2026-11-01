"""Bind a progress-only model reply to its latest actual task-status tool read."""
import json

from src.bank_tasks.progress import build, render
from src.bank_v1.common import canonical


def requested(result):
    if result.get('task_progress_answer'):
        return True
    try:
        return json.loads(result.get('text', '')).get('format') == 'task_progress_v1'
    except (ValueError, AttributeError):
        return False


def reject(result, reason):
    return {**result, 'ok': False, 'session_id': None,
            'error': '任务进度未能与本轮工具证据对应，请查看任务区及银行原操作。',
            'answer_validation': {'scope': 'task_progress', 'version': 'task-progress-1',
                                  'status': 'REJECTED', 'gate_applied': True, 'reason': reason}}


def validated_source(body, task_id):
    data = body['data']
    task, progress = data['task'], data['task_progress']
    if task['task_id'] != task_id or body.get('scope', 'bank') != 'bank':
        raise ValueError('WRONG_TASK_PROGRESS_SOURCE')
    rebuilt = build(task, progress['data']['operations'], body['observed_at'])
    if canonical(rebuilt) != canonical(progress):
        raise ValueError('INVALID_TASK_PROGRESS_SOURCE')
    return progress


def validate(result):
    try:
        answer = result.get('task_progress_answer') or json.loads(result['text'])
        if (set(answer) != {'format', 'task_id', 'source_id'} or answer['format'] != 'task_progress_v1'
                or not all(isinstance(answer[k], str) and answer[k] for k in ('task_id', 'source_id'))):
            raise ValueError('INVALID_TASK_PROGRESS_ANSWER')
        requests, completed, source, last_tool = {}, set(), None, None
        for row in result.get('trace', []):
            if not isinstance(row, dict):
                continue
            if row.get('kind') == 'request':
                call_id = row.get('call_id')
                if not call_id or call_id in requests:
                    raise ValueError('INVALID_TOOL_REQUEST')
                requests[call_id] = row
                # Any later task mutation invalidates a previous status snapshot.
                if row.get('name') in {'submit_task_plan', 'replan_task'}:
                    source = None
            elif row.get('kind') == 'response':
                call_id = row.get('call_id')
                request = requests.get(call_id)
                if (not request or call_id in completed or request.get('name') != row.get('tool')
                        or request.get('arguments') != row.get('args')):
                    raise ValueError('UNBOUND_TASK_TOOL_RESPONSE')
                completed.add(call_id)
                last_tool = row.get('tool')
                if row.get('tool') == 'task_status':
                    source = None
                    if (row.get('ok') is True and row.get('status') == 200
                            and row.get('args') == {'task_id': answer['task_id']}):
                        source = validated_source(row['response'], answer['task_id'])
        if (set(requests) != completed or last_tool != 'task_status' or not source
                or answer['source_id'] != source['source_id']):
            raise ValueError('TASK_PROGRESS_NOT_LATEST')
        return {**result, 'text': render(source), 'task_progress_answer': answer,
                'task_progress_basis': source,
                'answer_validation': {'scope': 'task_progress', 'version': 'task-progress-1',
                                      'status': 'ACCEPTED', 'gate_applied': True,
                                      'source_id': source['source_id']}}
    except (ValueError, KeyError, TypeError, AttributeError):
        return reject(result, 'INVALID_OR_STALE_TASK_PROGRESS')


def refresh(result, call_tool):
    """Host-only display refresh, explicitly separate from the model's observation."""
    checked = validate(result)
    if not checked.get('ok'):
        return checked
    try:
        task_id = checked['task_progress_answer']['task_id']
        body = call_tool('task_status', {'task_id': task_id})
        source = validated_source(body, task_id)
        changed = source['source_id'] != checked['task_progress_basis']['source_id']
        prefix = '系统已在 AI 查询后重新读取任务状态，以下为更新后的进度。\n' if changed else ''
        return {**checked, 'text': prefix + render(source), 'task_progress_refresh': {
            'actor': 'SYSTEM_READ_ONLY', 'model_source_id': checked['task_progress_basis']['source_id'],
            'display_source': source}}
    except Exception:
        return reject(checked, 'TASK_PROGRESS_REFRESH_FAILED')
