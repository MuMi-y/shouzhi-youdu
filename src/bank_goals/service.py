"""Trusted goal workflow. Selection, bank preparation and receipt linking are separate."""
from datetime import datetime
import json
from pathlib import Path
from contextlib import contextmanager

from src.bank_v1 import finance, operations, basis_guard, security
from src.bank_v1.common import DomainError, business_date, canonical, identifier
from src.bank_v1.storage import connection
from . import contracts, projection, storage


class GoalService:
    def __init__(self, db_path, *, bank, clock):
        self.db_path, self.bank, self.clock = Path(db_path), bank, clock
        storage.initialize(self.db_path)
        with connection(bank.db_path, True) as db:
            basis_guard.initialize(db)

    def reserve_reference(self):
        """A named goal preference, never an implicit global reserve policy."""
        with connection(self.db_path) as db:
            goal = storage.goal(db)
        if goal is None:
            return None
        return {'goal_id': goal['goal_id'], 'revision': goal['revision'],
                'title': goal['profile']['title'], 'floor_minor': goal['profile']['reserve_floor_minor']}

    @contextmanager
    def reserve_scope(self):
        """Serialize plan preparation with goal writes, always goal then bank."""
        with connection(self.db_path, True) as db:
            goal = storage.goal(db)
            value = None if goal is None else {
                'goal_id': goal['goal_id'], 'revision': goal['revision'],
                'title': goal['profile']['title'], 'floor_minor': goal['profile']['reserve_floor_minor']}
            yield value

    def _now(self):
        now = self.clock()
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise ValueError('clock must return an aware datetime')
        return now

    def _snapshot(self, now):
        with connection(self.bank.db_path) as db:
            return finance.forecast_snapshot(db, now)

    @staticmethod
    def _goal(db, revision=None):
        goal = storage.goal(db)
        if goal is None:
            raise DomainError('GOAL_MISSING', '请先核对并保存一个目标。')
        if revision is not None and goal['revision'] != revision:
            raise DomainError('VERSION_CONFLICT', '目标已修改，请刷新后重新核对。')
        return goal

    @staticmethod
    def _assessment(db, assessment_id):
        row = db.execute('SELECT * FROM assessments WHERE assessment_id=?', (assessment_id,)).fetchone()
        if row is None:
            raise DomainError('NOT_FOUND', '方案记录不存在，请重新计算。')
        return {**dict(row), 'result': json.loads(row['result_json'])}

    @staticmethod
    def _choice(db, choice_id):
        row = db.execute('SELECT * FROM choices WHERE choice_id=?', (choice_id,)).fetchone()
        if row is None:
            raise DomainError('NOT_FOUND', '选择记录不存在。')
        return dict(row)

    @staticmethod
    def _bank_key(choice):
        return 'goal-choice:' + choice['choice_id']

    def _operation(self, bank_db, choice, now):
        # Recovery only looks up the original intent. It never creates an operation.
        row = bank_db.execute("SELECT operation_id FROM operations WHERE idempotency_key=? AND subject_id='U1'",
                              (self._bank_key(choice),)).fetchone()
        if row is None:
            if choice['operation_id']:
                raise DomainError('LINK_INCONSISTENT', '原操作缺失，请检查整组数据库恢复，不能重新办理。')
            return None
        if choice['operation_id'] and choice['operation_id'] != row[0]:
            raise DomainError('LINK_INCONSISTENT', '操作绑定不一致，请核对恢复资料。')
        return operations.get_operation(bank_db, row[0], now)

    def _sync(self, db, choice, now):
        with connection(self.bank.db_path) as bank_db:
            op = self._operation(bank_db, choice, now)
        if op is None:
            return choice
        state = op['status']
        receipt = op['receipt']
        if state == 'SUCCEEDED':
            state = 'LINKED'
            # Only the latest choice may update the focus pointer; historical reconciliation stays historical.
            if storage.latest_choice(db)['choice_id'] == choice['choice_id']:
                db.execute("UPDATE goals SET linked_arrangement_id=?,linked_version=? WHERE goal_id='focus'",
                           (receipt['arrangement_id'], receipt['version']))
        elif state == 'PENDING':
            state = 'EXPIRED' if now >= datetime.fromisoformat(op['expires_at']) else 'WAITING_CONFIRMATION'
        db.execute('UPDATE choices SET operation_id=?,state=?,receipt_json=? WHERE choice_id=?',
                   (op['operation_id'], state, canonical(receipt) if receipt else None, choice['choice_id']))
        return self._choice(db, choice['choice_id'])

    def _settle_previous(self, db, now):
        choice = storage.latest_choice(db)
        if choice:
            choice = self._sync(db, choice, now)
            if choice['state'] == 'WAITING_CONFIRMATION':
                raise DomainError('PENDING_CHOICE', '请先确认或拒绝原安排卡，再修改目标或选择其他方案。')
        return choice

    def action(self, name, body):
        contracts.validate(name, body)
        # One writer serializes goal revisions and selections; bank locking always follows this lock.
        with connection(self.db_path, True) as db:
            now = self._now()
            if name == 'save':
                return self._save(db, body, now)
            if name == 'compare':
                return self._compare(db, body, now)
            if name == 'select':
                return self._select(db, body, now)
            choice = self._choice(db, body['choice_id'])
            if name == 'reconcile':
                return self._sync(db, choice, now)
            return self._prepare(db, choice, now)

    def _save(self, db, body, now):
        self._settle_previous(db, now)
        current = storage.goal(db)
        revision = current['revision'] if current else 0
        if body['expected_revision'] != revision:
            raise DomainError('VERSION_CONFLICT', '目标已修改，请刷新后重新核对。')
        profile = body['profile']
        contracts.validate_dates(profile, business_date(now))
        if current and current['profile'] == profile:
            return current
        db.execute('''INSERT INTO goals VALUES('focus','U1',?,?,NULL,NULL,?)
            ON CONFLICT(goal_id) DO UPDATE SET revision=excluded.revision,
            profile_json=excluded.profile_json,updated_at=excluded.updated_at''',
            (revision+1, canonical(profile), now.isoformat()))
        return storage.goal(db)

    def _compare(self, db, body, now):
        self._settle_previous(db, now)
        goal = self._goal(db, body['expected_revision'])
        snapshot = self._snapshot(now)
        result = projection.compare(goal, snapshot)
        assessment_id = identifier('assessment')
        result['assessment_id'] = assessment_id
        db.execute('INSERT INTO assessments VALUES(?,?,?,?,?)',
                   (assessment_id, goal['revision'], result['bank_hash'], canonical(snapshot), canonical(result)))
        return result

    def _assert_basis(self, db, assessment, snapshot):
        self._goal(db, assessment['goal_revision'])
        if finance.snapshot_hash(snapshot) != assessment['bank_hash']:
            raise DomainError('STALE_GOAL_BASIS', '账本、安排或业务日期已变化，请重新计算并选择。')

    def _select(self, db, body, now):
        old = db.execute('SELECT * FROM choices WHERE idempotency_key=?', (body['idempotency_key'],)).fetchone()
        if old:
            assessment = self._assessment(db, old['assessment_id'])
            if (old['assessment_id'], old['option_id'], bool(old['wait_for_income']), assessment['goal_revision']) != (
                    body['assessment_id'], body['option_id'], body['wait_for_income'], body['expected_revision']):
                raise DomainError('IDEMPOTENCY_CONFLICT', '重复请求编号不能改变选择。')
            return self._sync(db, dict(old), now)
        self._settle_previous(db, now)
        self._goal(db, body['expected_revision'])
        assessment = self._assessment(db, body['assessment_id'])
        self._assert_basis(db, assessment, self._snapshot(now))
        option = next((o for o in assessment['result']['options'] if o['option_id'] == body['option_id']), None)
        if option is None:
            raise DomainError('INVALID_ARGUMENTS', '请选择已计算的候选方案。')
        if db.execute('SELECT 1 FROM choices WHERE assessment_id=?', (body['assessment_id'],)).fetchone():
            raise DomainError('CHOICE_CONFLICT', '这一组方案已经选过，请重新计算后再选择。')
        if body['wait_for_income'] and not option['forecast']['income_conditions']:
            raise DomainError('NO_INCOME_CONDITION', '当前方案没有有效预计收入；不能把未知收入当作等待条件。')
        choice_id = identifier('choice')
        db.execute("INSERT INTO choices VALUES(?,?,?,?,?,NULL,'SELECTED',NULL,?)",
                   (choice_id, body['assessment_id'], body['option_id'], int(body['wait_for_income']),
                    body['idempotency_key'], now.isoformat()))
        return self._choice(db, choice_id)

    def _prepare(self, db, choice, now):
        choice = self._sync(db, choice, now)
        if choice['operation_id']:
            return choice  # Repeated request returns original operation; no re-prepare.
        if storage.latest_choice(db)['choice_id'] != choice['choice_id']:
            raise DomainError('STALE_CHOICE', '此选择已被后续选择替代。')
        assessment = self._assessment(db, choice['assessment_id'])
        goal = self._goal(db, assessment['goal_revision'])
        option = next(o for o in assessment['result']['options'] if o['option_id'] == choice['option_id'])
        if choice['wait_for_income']:
            raise DomainError('WAITING_FOR_INCOME', '已选择等待收入。实际到账并核对关联后，请重算选择；当前不保存银行安排。')
        args = {'title': goal['profile']['title'], 'kind': 'expense', 'amount_minor': option['amount_minor'],
                'due_date': option['due_date'], 'status': 'active', 'idempotency_key': self._bank_key(choice)}
        replaced = assessment['result']['replaced_arrangement']
        if replaced:
            args.update(arrangement_id=replaced['arrangement_id'], expected_version=replaced['version'])
        # Bank prepare + basis guard are atomic. Goal association is deliberately a later commit.
        with connection(self.bank.db_path, True) as bank_db:
            security.assert_unlocked(bank_db)
            self._assert_basis(db, assessment, finance.forecast_snapshot(bank_db, now))
            op = operations.prepare(bank_db, 'arrangement', args, now)
            basis_guard.bind(bank_db, op['operation_id'], assessment['bank_hash'], choice['choice_id'])
        db.execute("UPDATE choices SET operation_id=?,state='WAITING_CONFIRMATION' WHERE choice_id=?",
                   (op['operation_id'], choice['choice_id']))
        return self._choice(db, choice['choice_id'])

    def state(self):
        now = self._now()
        with connection(self.db_path) as db:
            goal = storage.goal(db)
            choice = storage.latest_choice(db)
            row = db.execute('SELECT result_json FROM assessments ORDER BY rowid DESC LIMIT 1').fetchone()
            assessment = json.loads(row[0]) if row else None
            with connection(self.bank.db_path) as bank_db:
                snapshot = finance.forecast_snapshot(bank_db, now)
                op = self._operation(bank_db, choice, now) if choice else None
            if assessment:
                assessment['stale'] = (assessment['goal_revision'] != goal['revision'] or
                                        assessment['bank_hash'] != finance.snapshot_hash(snapshot))
            if choice:
                choice['operation'] = op
                choice['assessment'] = self._assessment(db, choice['assessment_id'])['result']
                choice['basis_stale'] = (choice['assessment']['goal_revision'] != goal['revision'] or
                                         choice['assessment']['bank_hash'] != finance.snapshot_hash(snapshot))
                if op and op['status'] == 'SUCCEEDED' and choice['state'] != 'LINKED':
                    choice['display_state'] = 'LINK_PENDING'
                elif op and op['status'] == 'PENDING' and now >= datetime.fromisoformat(op['expires_at']):
                    choice['display_state'] = 'EXPIRED'
                else:
                    choice['display_state'] = op['status'] if op and op['status'] == 'REJECTED' else choice['state']
                if not op and choice['basis_stale']:
                    choice['display_state'] = 'STALE'
            fulfillment = None
            if goal and goal['linked_arrangement_id']:
                match = next((m for m in snapshot['matches'] if m['arrangement_id'] == goal['linked_arrangement_id']), None)
                arrangement = next((a for a in snapshot['arrangements'] if a['arrangement_id'] == goal['linked_arrangement_id']), None)
                fulfillment = {'arrangement': arrangement, 'match': match,
                               'status': 'EXPENSE_MATCHED' if match else 'ARRANGEMENT_ONLY'}
                if choice and op and op.get('receipt') and arrangement and arrangement['version'] != op['receipt'].get('version'):
                    choice['display_state'] = 'PLAN_CHANGED'
                    choice['current_arrangement'] = arrangement
            return {'enabled': True, 'mode': 'DETERMINISTIC_NO_MODEL', 'goal': goal, 'assessment': assessment,
                    'choice': choice, 'fulfillment': fulfillment, 'business_date': snapshot['business_date']}
