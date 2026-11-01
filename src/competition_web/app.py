"""Trusted browser actions extend V2 while retaining its origin/CSRF boundary."""
from datetime import datetime, timedelta, timezone
from pathlib import Path

from flask import jsonify

from src.bank_v1 import Service
from src.bank_v1_web.app import create_app as base_app, json_object, RequestError, require_model_ready
from src.bank_tasks import TaskError
from src.bank_insights import InsightError
from .service import CompetitionService
from src.bank_v1.plans import PlanService


def create_app(data_dir, *, clock=None, bank=None, history=None, security_policy=None, goals_enabled=False,
               goal_followup=False, goal_ai=False, service_lock=None):
    clock = clock or (lambda: datetime.now(timezone(timedelta(hours=8))))
    directory = Path(data_dir).resolve()
    directory.mkdir(parents=True, exist_ok=True)
    from src.bank_v1_web.handoff_store import service_ownership, backup_before_migration
    owner, owned = service_ownership(directory,service_lock)
    from src.bank_v1_web.jobs import JobStore
    jobs = None
    try:
        backup_before_migration(directory)
        bank = bank or Service(directory / 'bank.sqlite3', clock=clock, security_policy=security_policy)
        jobs = JobStore(directory / 'jobs.sqlite3', clock=clock, handoff=True,service_lock=owner)
        jobs.handoff.service_lock = owner if owned else None
        facade = CompetitionService(directory, bank=bank, clock=clock, goals_enabled=goals_enabled,
                                    goal_followup=goal_followup, goal_ai=goal_ai, handoff_store=jobs.handoff)
        app = base_app(directory, clock=clock, service=facade, history=history, jobs=jobs)
    except BaseException:
        if jobs and jobs.handoff:
            jobs.handoff.release_owner()
        if owned:
            owner.close()
        raise
    app.extensions['service_ownership'] = owner
    app.extensions['competition'] = facade
    plans = PlanService(bank, goal_scope=facade.goals.reserve_scope if facade.goals else None)

    @app.get('/api/competition/plans')
    def plan_list():
        return jsonify(plans.state())

    @app.post('/api/competition/plans/<action>')
    def plan_action(action):
        if action not in {'preview', 'prepare', 'spending'}:
            raise RequestError('UNKNOWN_ACTION', '不支持此计划操作。', 404)
        body = json_object()
        if action == 'prepare':
            return jsonify(facade.host_action('plans.prepare',body,lambda:plans.prepare(body),effect_class='BANK_PREPARE'))
        return jsonify(getattr(plans, action)(body))
    app.view_functions['health'] = lambda: jsonify(app='youshu-competition', version='1', ok=True)

    @app.get('/api/competition/goals')
    def goals():
        return jsonify(facade.goals.state() if facade.goals else {'enabled': False})

    @app.post('/api/competition/goals/<action>')
    def goal_action(action):
        if facade.goals is None:
            raise RequestError('GOALS_DISABLED', '目标入口暂未开启，已有安排和回执仍保留。', 409)
        body = json_object()
        return jsonify(facade.host_action('goals.'+action,body,lambda:facade.goals.action(action,body),effect_class='GOAL_ACTION'))

    @app.post('/api/competition/goal-followup/retry')
    def retry_goal_analysis():
        require_model_ready(app)
        if facade.goal_followup is None:
            raise RequestError('GOALS_DISABLED', '目标持续服务未启用。', 409)
        body = json_object()
        if set(body) != {'expected_check_id'} or type(body['expected_check_id']) is not str:
            raise RequestError('INVALID_ARGUMENTS', '请提供当前未完成分析的编号。')
        check = facade.host_action('goal-followup.retry',body,lambda:facade.goal_followup.retry(body['expected_check_id']))
        facade.goal_followup.enqueue(app.extensions['bank_v1']['jobs'], observe=False)
        return jsonify(check=check, service_health=facade.goal_followup.state()['service_health'])

    @app.get('/api/competition/tasks/<task_id>')
    def task(task_id):
        return jsonify(facade.tasks.get(task_id))

    @app.post('/api/competition/tasks/<task_id>/<action>')
    def task_action(task_id, action):
        body = json_object()
        if action in ('pause', 'resume', 'cancel'):
            if body:
                raise RequestError('INVALID_ARGUMENTS', '这个动作不接受额外参数。')
            return jsonify(facade.host_action('tasks.'+action,{'task_id':task_id},lambda:getattr(facade.tasks, action)(task_id),effect_class='TASK_WRITE'))
        if action == 'retry' and set(body) == {'node_id'} and type(body['node_id']) is str:
            return jsonify(facade.host_action('tasks.retry',{'task_id':task_id,**body},lambda:facade.tasks.retry(task_id, body['node_id']),effect_class='TASK_WRITE'))
        if action == 'handoff' and set(body) == {'reason'} and type(body['reason']) is str and 1 <= len(body['reason']) <= 500:
            return jsonify(facade.host_action('tasks.handoff',{'task_id':task_id,**body},lambda:facade.tasks.handoff(task_id, body['reason']),effect_class='TASK_WRITE'))
        raise RequestError('INVALID_ARGUMENTS', '任务动作或参数无效。')

    @app.post('/api/competition/candidate-feedback')
    def candidate_feedback():
        body=json_object()
        return jsonify(facade.host_action('candidate_feedback',body,lambda:facade.insights.user_action('candidate_feedback',body),effect_class='INSIGHT_WRITE'))

    @app.post('/api/competition/insights-check')
    def insights_check():
        if json_object():
            raise RequestError('INVALID_ARGUMENTS', '检查不接受额外参数。')
        return jsonify(facade.host_action('check_insights',{},lambda:facade.insights.call_tool('check_insights', {}),effect_class='INSIGHT_WRITE'))

    @app.post('/api/competition/security/<action>')
    def security_action(action):
        allowed = {'verify': 'verify_operation', 'unlock': 'security_unlock'}
        if action not in allowed:
            raise RequestError('UNKNOWN_ACTION', '不支持的验证动作。', 404)
        return jsonify(facade.user_action(allowed[action], json_object()))

    @app.errorhandler(TaskError)
    @app.errorhandler(InsightError)
    def task_error(error):
        return jsonify(error={'code': error.code, 'message': str(error)}), 409

    return app
