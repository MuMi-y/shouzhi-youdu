"""One host-only display boundary for product and standalone model evaluation."""


def check(result, service):
    if not result.get('ok'):
        return result
    if result.get('request_delivery'):
        from src.bank_requests.delivery import recheck
        return recheck(result, service)
    if result.get('goal_impact_basis'):
        from src.bank_goals.impact_answer import recheck
        return recheck(result, getattr(service, 'goal_tools', None))
    if result.get('bank_service_basis'):
        from src.bank_evidence.service_answer import recheck
        return recheck(result, service.call_tool)
    if result.get('bank_facts_basis'):
        from src.bank_evidence.freshness import recheck
        return recheck(result, service.call_tool)
    if result.get('task_progress_answer'):
        from .task_progress_answer import refresh
        return refresh(result, service.call_tool)
    if result.get('goal_preview_basis'):
        from src.bank_goals.preview_answer import recheck
        return recheck(result, getattr(service, 'goal_tools', None))
    if result.get('goal_basis_hash') and getattr(service, 'goal_tools', None):
        current = service.goal_tools.call('goal_context', {'goal_id': 'focus'})
        if current['data']['basis_hash'] != result['goal_basis_hash']:
            return {**result, 'ok': False, 'session_id': None, 'error': '目标或账本已改变，旧解释未采用。'}
    return result
