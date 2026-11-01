"""Trusted manual plan editing over the existing bank ledger and confirmation boundary."""
from copy import deepcopy
from contextlib import contextmanager
import hashlib
from . import finance, plan_projection, operations, schemas, basis_guard, security
from .common import DomainError, canonical
from .storage import connection


class PlanService:
    def __init__(self, bank, *, goal_reader=None, goal_scope=None):
        self.bank = bank
        self.goal_reader = goal_reader
        self.goal_scope = goal_scope

    @contextmanager
    def _goal(self):
        # Production holds the goal scope before taking a bank lock. Legacy
        # callbacks are sampled once here, never inside a bank transaction.
        if self.goal_scope:
            with self.goal_scope() as value:
                yield deepcopy(value)
        else:
            yield deepcopy(self.goal_reader()) if self.goal_reader else None

    def _forecast(self, snapshot, through_date=None, proposed_expense_minor=0, proposed_date=None, *, goal=None):
        result = plan_projection.forecast(snapshot, through_date, proposed_expense_minor, proposed_date)
        result["reserve_assessment"] = None if goal is None else {
            **goal, "shortfall_minor": max(0, goal["floor_minor"]-result["lowest_balance_minor"]),
            "without_income_shortfall_minor": max(0, goal["floor_minor"]-result["without_future_income_lowest_balance_minor"])}
        return result

    def state(self):
        with self._goal() as goal:
            with connection(self.bank.db_path) as db:
                snapshot = finance.forecast_snapshot(db, self.bank._now())
            return {'business_date': snapshot['business_date'], 'arrangements': snapshot['arrangements'],
                    'forecast': self._forecast(snapshot, goal=goal)}

    @staticmethod
    def draft(body):
        if type(body) is not dict:
            raise DomainError('INVALID_ARGUMENTS', '计划内容须为对象。')
        schemas.validate('propose_arrangement', {**body, 'idempotency_key': 'validation-only'}, schemas.tool_specs())
        if 'idempotency_key' in body or body.get('status', 'active') not in {'active', 'cancelled'}:
            raise DomainError('INVALID_ARGUMENTS', '这里只能保存或取消计划；完成状态须核对真实流水。')
        result = {**body, 'status': body.get('status', 'active')}
        if not result['title'].strip():
            raise DomainError('INVALID_ARGUMENTS', '请填写计划名称。')
        if not result.get('arrangement_id') and result['status'] != 'active':
            raise DomainError('INVALID_ARGUMENTS', '新增计划须为有效状态。')
        return result

    def _preview(self, db, draft, now, goal=None):
        operations.validate_params(db, 'arrangement', draft)
        snapshot = finance.forecast_snapshot(db, now)
        original = next((a for a in snapshot['arrangements'] if a['arrangement_id'] == draft.get('arrangement_id')), None)
        if original and original['status'] != 'active':
            raise DomainError('PLAN_INACTIVE', '此计划已经完成或取消；如有新的需要，请新增计划。')
        if draft['status'] == 'active' and draft['due_date'] < snapshot['business_date']:
            raise DomainError('INVALID_ARGUMENTS', '请核对计划日期，使用今天或未来日期。')
        trial = deepcopy(snapshot)
        trial['arrangements'] = [a for a in trial['arrangements'] if a['arrangement_id'] != draft.get('arrangement_id')]
        trial['arrangements'].append({**draft, 'arrangement_id': draft.get('arrangement_id', 'unconfirmed-plan'),
                                      'version': draft.get('expected_version', 0) + 1})
        # Compare equal windows even when moving/cancelling the last distant plan.
        end = max(plan_projection.window(snapshot)['through_date'], plan_projection.window(trial)['through_date'])
        basis = finance.snapshot_hash(snapshot)
        token = hashlib.sha256(canonical({'basis': basis, 'draft': draft, 'goal': goal}).encode()).hexdigest()
        return {'draft': draft, 'original': original, 'basis_hash': basis, 'preview_token': token,
                'before': self._forecast(snapshot, end, goal=goal), 'after': self._forecast(trial, end, goal=goal),
                'notice': '这是计划预览，没有保存或付款。即使存在缺口也可记录意愿；保存后仍不自动付款。'}

    def preview(self, body):
        draft = self.draft(body)
        with self._goal() as goal:
            with connection(self.bank.db_path) as db:
                return self._preview(db, draft, self.bank._now(), goal)

    def prepare(self, body):
        if type(body) is not dict or set(body) != {'draft', 'preview_token', 'idempotency_key'}:
            raise DomainError('INVALID_ARGUMENTS', '请先预览并核对完整计划。')
        draft = self.draft(body['draft'])
        args = {**draft, 'idempotency_key': body['idempotency_key']}
        schemas.validate('propose_arrangement', args, schemas.tool_specs())
        if type(body['preview_token']) is not str or len(body['preview_token']) != 64:
            raise DomainError('INVALID_ARGUMENTS', '计划预览凭据无效，请重新比较。')
        with self._goal() as goal:
            with connection(self.bank.db_path, True) as db:
                now = self.bank._now()
                security.assert_unlocked(db)
                basis_guard.initialize(db)
                old = db.execute('SELECT operation_id FROM operations WHERE idempotency_key=?', (body['idempotency_key'],)).fetchone()
                if old:
                    guard = db.execute('SELECT choice_id FROM operation_basis_guards WHERE operation_id=?', (old[0],)).fetchone()
                    if not guard or guard[0] != 'manual-plan:' + body['preview_token']:
                        raise DomainError('IDEMPOTENCY_CONFLICT', '原请求内容不能替换，请重新核对。')
                    return operations.prepare(db, 'arrangement', args, now)
                preview = self._preview(db, draft, now, goal)
                if preview['preview_token'] != body['preview_token']:
                    raise DomainError('STALE_PLAN_BASIS', '余额、计划或日期已变化，请保留输入并重新预览。')
                op = operations.prepare(db, 'arrangement', args, now)
                basis_guard.bind(db, op['operation_id'], preview['basis_hash'], 'manual-plan:' + preview['preview_token'])
                return op

    def spending(self, body):
        if type(body) is not dict or set(body) != {'amount_minor', 'due_date'}:
            raise DomainError('INVALID_ARGUMENTS', '请填写拟支出金额和日期。')
        with self._goal() as goal:
            with connection(self.bank.db_path) as db:
                snapshot = finance.forecast_snapshot(db, self.bank._now())
            after = self._forecast(snapshot, body['due_date'], body['amount_minor'], body['due_date'], goal=goal)
            return {'before': self._forecast(snapshot, after['through_date'], goal=goal), 'after': after,
                    'notice': '仅比较花与不花，没有记录消费或扣款。'}
