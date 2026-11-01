"""Three explicit model tools. No goal confirmation, selection or banking authority."""
import json
from datetime import datetime
from jsonschema import Draft202012Validator
from src.bank_v1 import finance
from src.bank_v1 import plan_projection
from src.bank_v1.common import DomainError, canonical, identifier, business_date
from src.bank_v1.storage import connection
from . import contracts, storage, projection, preview, current_plan
from .evidence import PROFILE, TEXT, obj, digest, envelope


def tool_specs():
    from .impact_contract import specs
    return specs() + [
        {'name': preview.TOOL, 'description': '首次目标尚未确认且资料完整时，只读试算原计划/降额/延期及收入不到账情景，不保存任何目标或草案。优先用本工具，不必先保存再比较；已有目标使用goal_context。按goal_preview_answer_v1引用全部claim_catalog。',
         'parameters': obj({'profile': PROFILE})},
        {'name': 'goal_context', 'description': '读取已确认重点目标与当前三案比较。预计收入不是到账，合计不保证单账户可付款。按goal_answer_v1引用claim_catalog，claims可用{path,reference:true}引用本次source_id的完整条目，无需转抄value，不输出任意金额解释。',
         'parameters': obj({'goal_id': {'const': 'focus'}, 'check_id': TEXT}, ['goal_id'])},
        {'name': 'compare_goal_options', 'description': '按目标版本只读比较原计划、降额和延期；不选择、不确认、不保存银行安排。输出按goal_answer_v1引用具体路径。',
         'parameters': obj({'goal_id': {'const': 'focus'}, 'expected_revision': {'type': 'integer', 'minimum': 1}})},
        {'name': 'propose_goal', 'description': '保存未确认目标草案。完整字段不足先询问，不擅自推断。草案仅供用户采用到表单，不能作为已确认目标或银行安排。',
         'parameters': obj({'expected_revision': {'type': 'integer', 'minimum': 0}, 'profile': PROFILE,
                            'idempotency_key': {**TEXT, 'maxLength': 160}})}]


class GoalTools:
    def __init__(self, goals):
        self.goals = goals
        from src.bank_v1 import projection_journal
        with connection(goals.bank.db_path, True) as bank_db:
            projection_journal.initialize(bank_db, goals._now())
        with connection(goals.db_path, True) as db:
            db.execute('''CREATE TABLE IF NOT EXISTS goal_drafts(draft_id TEXT PRIMARY KEY,
                expected_revision INTEGER NOT NULL, profile_json TEXT NOT NULL,
                idempotency_key TEXT UNIQUE NOT NULL, created_at TEXT NOT NULL)''')

    def capture(self, db, now, *, capture_meta=None):
        goal = self.goals._goal(db)
        choice = storage.latest_choice(db)
        with connection(self.goals.bank.db_path) as bank_db:
            snapshot = finance.forecast_snapshot(bank_db, now)
            op = self.goals._operation(bank_db, choice, now) if choice else None
            if capture_meta is not None:
                from . import impact_basis
                capture_meta.update(impact_basis.capture(self.goals, db, bank_db, now))
        choice_state = choice['state'] if choice else 'NONE'
        if op:
            choice_state = op['status']
            if op['status'] == 'SUCCEEDED':
                choice_state = 'LINKED' if choice['state'] == 'LINKED' else 'LINK_PENDING'
            elif op['status'] == 'PENDING' and now >= datetime.fromisoformat(op['expires_at']):
                choice_state = 'EXPIRED'
        basis = digest({'goal': goal, 'choice': choice, 'operation': op, 'bank_hash': finance.snapshot_hash(snapshot),
                        'choice_state': choice_state})
        facts_hash = digest({'goal': goal, 'choice': choice, 'operation': op,
                             'bank': {k: v for k, v in snapshot.items() if k != 'business_date'}})
        status, options, incomes = 'READY', [], []
        end = plan_projection.window(snapshot)['through_date']
        if choice_state == 'LINK_PENDING':
            status = 'LINK_PENDING'
        else:
            try:
                result = projection.compare(goal, snapshot)
                end = result['window_end']
                for option in result['options']:
                    f = option['forecast']
                    options.append({k: option[k] for k in ('option_id', 'amount_minor', 'due_date', 'status')} | {
                        'primary': {'minimum_minor': f['lowest_balance_minor'], 'date': f['lowest_balance_date'],
                                    'reserve_shortfall_minor': option['reserve_shortfall_minor']},
                        'without_income': {'minimum_minor': f['without_future_income_lowest_balance_minor'],
                            'date': f['without_future_income_lowest_balance_date'],
                            'reserve_shortfall_minor': option['without_income_reserve_shortfall_minor']}})
                incomes = result['options'][0]['forecast']['income_conditions']
            except DomainError as error:
                if error.code not in {'INVALID_ARGUMENTS', 'GOAL_NOT_ACTIVE'}:
                    raise
                status = 'DATE_REVIEW_REQUIRED' if error.code == 'INVALID_ARGUMENTS' else error.code
        chosen = current_plan.capture(choice, choice_state, op,
            self.goals._assessment(db, choice['assessment_id']) if choice else None, goal, snapshot)
        if chosen and chosen['projection'] and not options:
            end = plan_projection.window(snapshot, max(snapshot['business_date'], goal['profile']['latest_date']))['through_date']
            incomes = plan_projection.forecast(snapshot, end)['income_conditions']
        return {'goal_id': 'focus', 'revision': goal['revision'], 'basis_hash': basis, 'facts_hash': facts_hash,
            'business_date': snapshot['business_date'], 'profile': goal['profile'], 'status': status,
            'options': options, 'income_conditions': incomes, 'choice_state': choice_state,
            'current_total_minor': snapshot['accounts']['total_balance_minor'], 'current_plan': chosen,
            'coverage': {'window_end': end, 'outside_window': [{k: a[k] for k in ('arrangement_id','title','due_date')}
                for a in snapshot['arrangements'] if a['status'] == 'active' and a['due_date'] > end]}}

    def call(self, name, args, *, impact_context=None):
        definition = next((s for s in tool_specs() if s['name'] == name), None)
        if not definition or list(Draft202012Validator(definition['parameters']).iter_errors(args)):
            raise DomainError('INVALID_ARGUMENTS', '目标工具参数无效。')
        if 'expected_revision' in args and type(args['expected_revision']) is not int:
            raise DomainError('INVALID_ARGUMENTS', '版本必须是整数。')
        if name == 'propose_goal':
            return self.propose(args)
        if name == preview.TOOL:
            return self.preview(args['profile'])
        if name in {'compare_goal_impact', 'explain_goal_changes'}:
            from .impact import build
            return build(self.goals, name, args, impact_context=impact_context)
        with connection(self.goals.db_path) as db:
            data = self.capture(db, self.goals._now())
            if name == 'compare_goal_options' and args['expected_revision'] != data['revision']:
                raise DomainError('VERSION_CONFLICT', '目标已改变，请重新读取。')
            if args.get('check_id'):
                row = db.execute('SELECT * FROM goal_checks ORDER BY rowid DESC LIMIT 1').fetchone()
                if row is None or row['check_id'] != args['check_id'] or row['basis_hash'] != data['basis_hash']:
                    raise DomainError('STALE_GOAL_CHECK', '目标检查已过期，请使用最新检查。')
            return envelope(data, name)

    def preview(self, profile):
        contracts.validate('save', {'expected_revision': 0, 'profile': profile})
        with connection(self.goals.db_path) as db:
            if storage.goal(db) is not None:
                raise DomainError('GOAL_EXISTS', '已有确认目标，请读取目标后按现有目标比较。')
            with connection(self.goals.bank.db_path) as bank_db:
                snapshot = finance.forecast_snapshot(bank_db, self.goals._now())
            return preview.build(profile, snapshot)

    def propose(self, args):
        body = {k: args[k] for k in ('expected_revision', 'profile')}
        contracts.validate('save', body)
        with connection(self.goals.db_path, True) as db:
            now = self.goals._now()
            contracts.validate_dates(body['profile'], business_date(now))
            goal = storage.goal(db)
            if args['expected_revision'] != (goal['revision'] if goal else 0):
                raise DomainError('VERSION_CONFLICT', '目标版本变化，草案未保存。')
            old = db.execute('SELECT * FROM goal_drafts WHERE idempotency_key=?', (args['idempotency_key'],)).fetchone()
            if old:
                if old['expected_revision'] != args['expected_revision'] or old['profile_json'] != canonical(args['profile']):
                    raise DomainError('IDEMPOTENCY_CONFLICT', '同一草案编号不能改变内容。')
                draft_id = old['draft_id']
            else:
                draft_id = identifier('draft')
                db.execute('INSERT INTO goal_drafts VALUES(?,?,?,?,?)', (draft_id, args['expected_revision'],
                    canonical(args['profile']), args['idempotency_key'], now.isoformat()))
            return {'scope': 'goal_draft', 'data': {'draft_id': draft_id, 'status': 'UNCONFIRMED',
                    'notice': '草案已保存到目标区，请用户核对表单后确认；没有更改目标或银行安排。'}}

    def drafts(self):
        with connection(self.goals.db_path) as db:
            rows = db.execute('SELECT * FROM goal_drafts ORDER BY rowid DESC LIMIT 3').fetchall()
            return [{**dict(r), 'profile': json.loads(r['profile_json'])} for r in rows]
