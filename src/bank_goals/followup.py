"""Persist goal checks; the existing pump/worker owns scheduling and execution."""
from datetime import date
import json
from src.bank_v1.common import DomainError, canonical, identifier
from src.bank_v1.storage import connection
from . import storage
from . import service_health
from . import impact_basis
from .evidence import envelope, validate_answer


def historical_explanation(check, current_basis, current_revision):
    """A dated display record, never evidence for current decisions or another model call."""
    if not check or check['state'] != 'ACCEPTED':
        return None
    result = check.get('result') or {}
    if (result.get('ok') is not True or not isinstance(result.get('text'), str) or
            not result['text'].strip() or result.get('execution_mode') == 'OFFLINE_STUB'):
        return None
    return {'check_id': check['check_id'], 'created_at': check['created_at'],
        'business_date': check['context']['data']['business_date'], 'goal_revision': check['goal_revision'],
        'text': result['text'], 'basis_matches_current': check['basis_hash'] == current_basis,
        'same_goal_revision': check['goal_revision'] == current_revision}


def signature(data):
    from .date_review import context
    review = context(data)
    days = (date.fromisoformat(data['profile']['preferred_date']) - date.fromisoformat(data['business_date'])).days
    plan = data.get('current_plan')
    current = None if not plan else {'choice_id': plan['choice_id'], 'notice_state': plan['notice_state'],
        'arrangement': plan['arrangement'],
        'reserve_breach': {branch: bool(value['reserve_shortfall_minor'])
                           for branch, value in (plan['projection'] or {}).items()}}
    return {'revision': data['revision'], 'status': data['status'],
            'options': [[o['option_id'], o['status']] for o in data['options']],
            'income_conditions': data['income_conditions'], 'choice_state': data['choice_state'],
            'deadline': 'PAST' if days < 0 else 'WITHIN_THREE_DAYS' if days <= 3 else 'LATER',
            'current_plan': current,
            'date_review': {key: review[key] for key in ('reason', 'reference_date')} if review else None}


def check_prompt(check_id):
    return (f'重点目标检查 {check_id}：只调用 goal_context，参数 goal_id=focus、check_id={check_id}。'
            '根据返回claim_catalog选择有用字段，必须包括status。若current_plan非空，首先引用current_plan说明已选/已保存状态；'
            '它有projection时必须引用current_plan.primary和current_plan.without_income，先解释当前安排再讨论候选调整。'
            '没有current_plan时必须引用原计划primary/without_income两情景（存在时）。已选择后不要继续说尚未选择。'
            '若claim_catalog含date_review，必须引用它及全部income_conditions说明日期与收入条件，并选择CHECK_PLAN_DATE。'
            '最终只输出JSON：{"format":"goal_answer_v1","source_id":"工具source_id",'
            '"claims":[{"path":"工具claim_catalog中的path","reference":true}],'
            '"next_step":"从allowed_next_steps选择"}。reference=true表示引用该source_id下的完整工具条目；'
            '金额、日期、回执由系统从已核验工具结果呈现，不转抄value。'
            '不使用Markdown围栏或自由文本，不办理、选择或确认。')


class GoalFollowup:
    def __init__(self, tools, *, ai_enabled=False):
        self.tools, self.goals, self.ai_enabled = tools, tools.goals, ai_enabled
        with connection(self.goals.db_path, True) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS goal_checks(check_id TEXT PRIMARY KEY,
                basis_hash TEXT NOT NULL, goal_revision INTEGER NOT NULL, signature_json TEXT NOT NULL,
                reasons_json TEXT NOT NULL, context_json TEXT NOT NULL, state TEXT NOT NULL,
                result_json TEXT, created_at TEXT NOT NULL)''')
            service_health.initialize(db, self.goals._now().isoformat())
            impact_basis.initialize(db)

    @staticmethod
    def record(row):
        if not row:
            return None
        value = dict(row)
        for key in ('signature', 'reasons', 'context', 'result'):
            raw = value.pop(key + '_json')
            value[key] = json.loads(raw) if raw else None
        return value

    def observe(self, *, periodic_due=True):
        with connection(self.goals.db_path, True) as db:
            if storage.goal(db) is None:
                return None
            metadata = {}
            data = self.tools.capture(db, self.goals._now(), capture_meta=metadata)
            previous = self.record(db.execute('SELECT * FROM goal_checks ORDER BY rowid DESC LIMIT 1').fetchone())
            if previous and previous['basis_hash'] == data['basis_hash']:
                return previous
            if (previous and not periodic_due and
                    previous['context']['data'].get('facts_hash') == data['facts_hash']):
                return previous
            new_signature = signature(data)
            reasons = ['FIRST_CHECK'] if not previous else [key for key, value in new_signature.items()
                if previous['signature'].get(key) != value]
            if not reasons and previous and previous['state'] in {'PENDING', 'RUNNING'}:
                reasons = ['PENDING_EXPLANATION_REFRESHED']
            state = ('PENDING' if self.ai_enabled else 'NO_MODEL') if reasons else 'QUIET'
            db.execute("UPDATE goal_checks SET state='STALE' WHERE state IN ('PENDING','RUNNING')")
            check_id = identifier('goalcheck')
            db.execute('INSERT INTO goal_checks VALUES(?,?,?,?,?,?,?,?,?)',
                (check_id, data['basis_hash'], data['revision'], canonical(new_signature), canonical(reasons),
                 canonical(envelope(data, 'goal_context')), state, None, self.goals._now().isoformat()))
            impact_basis.save(db, check_id, metadata)
            if state == 'PENDING':
                service_health.requested(db, check_id, reasons, self.goals._now().isoformat())
            return self.record(db.execute('SELECT * FROM goal_checks WHERE check_id=?', (check_id,)).fetchone())

    def state(self):
        with connection(self.goals.db_path) as db:
            check = self.record(db.execute('SELECT * FROM goal_checks ORDER BY rowid DESC LIMIT 1').fetchone())
            goal = storage.goal(db)
            current_basis = self.tools.capture(db, self.goals._now())['basis_hash'] if goal else None
            stale = bool(check and goal and check['basis_hash'] != current_basis)
            accepted = self.record(db.execute("SELECT * FROM goal_checks WHERE state='ACCEPTED' ORDER BY rowid DESC LIMIT 1").fetchone())
            history = historical_explanation(accepted, current_basis, goal['revision'] if goal else None)
            return {'enabled': True, 'ai_enabled': self.ai_enabled, 'check': check, 'stale': stale,
                    'last_explanation': history,
                    'service_health': service_health.view(db, check, ai_enabled=self.ai_enabled, active=goal is not None),
                    'event_handling': 'GOAL_FOLLOWUP' if goal else 'BANK_EVENTS'}

    def retry(self, expected_check_id):
        """Trusted UI only: one idempotent, read-only recovery per pending episode."""
        if not self.ai_enabled:
            raise DomainError('GOAL_AI_DISABLED', '目标AI未启用，暂不能重新分析。')
        with connection(self.goals.db_path, True) as db:
            health = service_health.read(db)
            if health['retry_check_id'] and expected_check_id == health['retry_parent_check_id']:
                return self.record(db.execute('SELECT * FROM goal_checks WHERE check_id=?',
                                              (health['retry_check_id'],)).fetchone())
            latest = self.record(db.execute('SELECT * FROM goal_checks ORDER BY rowid DESC LIMIT 1').fetchone())
            if not health['pending_check_id'] or not health['last_attempt_check_id']:
                raise DomainError('NO_FAILED_GOAL_ANALYSIS', '没有需要恢复的目标分析。')
            if expected_check_id != health['last_attempt_check_id']:
                raise DomainError('STALE_GOAL_RECOVERY', '服务状态已变化，请刷新后查看。')
            if health['recovery_count'] >= 1:
                raise DomainError('GOAL_RECOVERY_LIMIT', '这次服务的恢复次数已用完，请检查失败原因。')
            if latest and latest['state'] in {'PENDING', 'RUNNING'}:
                raise DomainError('GOAL_ANALYSIS_PENDING', '已有目标分析等待或运行中。')
            if storage.goal(db) is None:
                raise DomainError('NO_GOAL', '请先保存目标。')
            metadata = {}
            data = self.tools.capture(db, self.goals._now(), capture_meta=metadata)
            check_id = identifier('goalcheck')
            now = self.goals._now().isoformat()
            db.execute('INSERT INTO goal_checks VALUES(?,?,?,?,?,?,?,?,?)',
                (check_id, data['basis_hash'], data['revision'], canonical(signature(data)),
                 canonical(['TRUSTED_READONLY_RECOVERY']), canonical(envelope(data, 'goal_context')),
                 'PENDING', None, now))
            impact_basis.save(db, check_id, metadata)
            db.execute('''UPDATE goal_service_health SET recovery_count=1,retry_check_id=?,
                retry_parent_check_id=?,updated_at=? WHERE id=1''', (check_id, expected_check_id, now))
            return self.record(db.execute('SELECT * FROM goal_checks WHERE check_id=?', (check_id,)).fetchone())

    def active(self):
        with connection(self.goals.db_path) as db:
            return storage.goal(db) is not None

    def enqueue(self, jobs, *, periodic_due=True, observe=True):
        if observe:
            check = self.observe(periodic_due=periodic_due)
        else:
            with connection(self.goals.db_path) as db:
                check = self.record(db.execute('SELECT * FROM goal_checks ORDER BY rowid DESC LIMIT 1').fetchone())
        if not check or not self.ai_enabled or check['state'] != 'PENDING':
            return
        job = jobs.add(check_prompt(check['check_id']), kind='goal',
                       event_key='goal:' + check['check_id'], session_id='', read_only=True)
        # Recover the interval between queue completion and result persistence without re-running AI.
        if job['status'] in {'DONE', 'FAILED', 'INTERRUPTED'}:
            self.finish(check['check_id'], job['result'] or {'ok': False, 'error': '上次目标分析中断。'})

    def start(self, check_id):
        with connection(self.goals.db_path, True) as db:
            latest = db.execute('SELECT * FROM goal_checks ORDER BY rowid DESC LIMIT 1').fetchone()
            current = self.tools.capture(db, self.goals._now())
            if (latest is None or latest['check_id'] != check_id or latest['state'] != 'PENDING'
                    or latest['basis_hash'] != current['basis_hash']):
                raise DomainError('STALE_GOAL_CHECK', '检查已被新事实替代，未启动模型。')
            db.execute("UPDATE goal_checks SET state='RUNNING' WHERE check_id=?", (check_id,))
            service_health.started(db, check_id, self.goals._now().isoformat())

    def finish(self, check_id, result):
        checked = validate_answer(result) if result.get('ok') else result
        with connection(self.goals.db_path, True) as db:
            row = db.execute('SELECT * FROM goal_checks WHERE check_id=?', (check_id,)).fetchone()
            latest = db.execute('SELECT check_id FROM goal_checks ORDER BY rowid DESC LIMIT 1').fetchone()
            data = self.tools.capture(db, self.goals._now())
            stale = (not row or not latest or latest[0] != check_id or row['basis_hash'] != data['basis_hash']
                     or (checked.get('ok') and checked.get('goal_basis_hash') != data['basis_hash']))
            if stale:
                checked = {**checked, 'ok': False, 'error': '旧目标解释未采用；目标或账本已变化。'}
            state = 'STALE' if stale else 'ACCEPTED' if checked.get('ok') else 'FAILED'
            # Explicit local stubs never become accepted real-model explanations or bank events.
            if not stale and checked.get('ok') and result.get('execution_mode') == 'OFFLINE_STUB':
                state = 'OFFLINE_CONTRACT_ONLY'
                checked = {**checked, 'ok': False, 'error': '仅离线契约验证，不是实际AI服务结果。'}
            if row:
                db.execute('UPDATE goal_checks SET state=?,result_json=? WHERE check_id=?',
                           (state, canonical(checked), check_id))
                service_health.finished(db, check_id, state, self.goals._now().isoformat(),
                                        is_latest=bool(latest and latest[0] == check_id))
            return checked

    def recover(self, jobs):
        with connection(self.goals.db_path, True) as db:
            rows = db.execute("SELECT check_id FROM goal_checks WHERE state='RUNNING'").fetchall()
            for row in rows:
                db.execute("UPDATE goal_checks SET state='INTERRUPTED' WHERE check_id=?", (row[0],))

