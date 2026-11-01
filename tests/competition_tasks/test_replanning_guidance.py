"""Guidance is a snapshot, never an authorization bypass. Only disposable banks."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from contextlib import closing
import tempfile
import unittest

from src.bank_insights.fixture import create_demo
from src.bank_tasks import TaskError
from src.bank_tasks.service import definition
from src.competition_web.service import CompetitionService, task_specs


def node(identifier, tool='accounts', args=None, parents=None):
    return {'id': identifier, 'tool': tool, 'args': args or {}, 'depends_on': parents or []}


def transfer(amount=50000):
    return node('pay', 'prepare_transfer', {'source_account': 'A', 'target_account': 'B',
        'amount_minor': amount}, ['balance'])


class ReplanningGuidanceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.now = datetime(2026, 10, 12, 4, tzinfo=timezone.utc)
        self.bank = create_demo(self.root / 'bank.sqlite3', clock=lambda: self.now)
        self.service = CompetitionService(self.root, bank=self.bank, clock=lambda: self.now)
        self.tasks = self.service.tasks

    def submit(self, amount=50000, key='case'):
        return self.service.call_tool('submit_task_plan', {'prompt': '先核对余额再准备转账',
            'title': '转账任务', 'nodes': [node('balance'), transfer(amount)],
            'idempotency_key': key})['data']['task']

    def status(self, tid):
        return self.service.call_tool('task_status', {'task_id': tid})['data']['task']

    def replan(self, task, amount=50000, nodes=None):
        return self.service.call_tool('replan_task', {'task_id': task['task_id'],
            'expected_version': task['plan_version'], 'reason': '用户明确改金额或修复未完成节点',
            'nodes': nodes or [node('balance'), transfer(amount)]})['data']['task']

    def error(self, code, callback):
        with self.assertRaises(TaskError) as caught: callback()
        self.assertEqual(caught.exception.code, code)

    def test_failed_9000_to_500_preserves_success_and_executes_once(self):
        before = self.bank.state()
        task = self.submit(900000); tid = task['task_id']
        self.tasks.tick(tid)
        failed = self.tasks.tick(tid)
        self.assertEqual(failed['state'], 'FAILED')
        self.assertEqual(failed['nodes'][1]['error']['code'], 'INSUFFICIENT_FUNDS')
        self.assertEqual(self.bank.state()['operations'], [])
        guidance = self.status(tid)['replan_guidance']
        self.assertEqual(guidance['eligibility'], 'CONDITIONAL')
        self.assertEqual(guidance['must_preserve_succeeded_node_ids'], ['balance'])
        self.assertEqual(guidance['reconciliation_on_changed_node_ids'], [])
        revised = self.replan(failed)
        self.assertEqual(revised['plan_version'], 2)
        self.assertEqual(revised['nodes'][0], failed['nodes'][0])
        self.assertEqual(revised['plan_history'][0]['nodes'], failed['nodes'])
        waiting = self.tasks.tick(tid)
        self.assertEqual(waiting['state'], 'WAITING_CONFIRMATION')
        self.assertEqual(self.bank.state()['transactions'], before['transactions'])
        op = waiting['nodes'][1]['result']['data']['operation']
        self.bank.user_action('confirm', {'operation_id': op['operation_id'], 'expected_summary': op['summary']})
        done = self.tasks.tick(tid)
        self.assertEqual(done['state'], 'SUCCEEDED')
        self.assertEqual(done['nodes'][0]['attempt'], 1)
        self.assertEqual(done['replan_guidance']['eligibility'], 'BLOCKED')
        after = self.bank.state()
        self.tasks.tick(tid)
        self.assertEqual(self.bank.state()['transactions'], after['transactions'])
        self.assertEqual(len(after['operations']), 1)
        balances = {a['account_id']: a['balance_minor'] for a in after['accounts']}
        self.assertEqual(balances, {'A': 434500, 'B': 150000})

    def test_snapshot_state_matrix_and_version_limit(self):
        tid = self.submit()['task_id']
        for state in ['READY', 'FAILED', 'WAITING_CONFIRMATION', 'WAITING_VERIFICATION',
                      'PAUSED', 'NEEDS_HUMAN', 'SUCCEEDED', 'CANCELLED']:
            for version in [1, 9, 10]:
                with self.subTest(state=state, version=version):
                    # Controlled state projection verifies display mapping only;
                    # actual transitions and guards are tested separately below.
                    with self.tasks._db(True) as db:
                        task = self.tasks._load(db, tid)
                        task['state'], task['plan_version'] = state, version
                        self.tasks._save(db, task)
                    g = self.status(tid)['replan_guidance']
                    blocked = state in {'SUCCEEDED', 'CANCELLED'} or version == 10
                    self.assertEqual(g['eligibility'], 'BLOCKED' if blocked else 'CONDITIONAL')
                    self.assertEqual(g['snapshot_plan_version'], version)
                    self.assertEqual(g['remaining_replans'], 10 - version)
                    self.assertEqual(g['current_auto_advance_paused'], state in {'PAUSED', 'NEEDS_HUMAN'})

    def test_get_is_read_only_and_guidance_not_persisted(self):
        task = self.submit()
        before = self.bank.state()
        self.tasks.call_tool = lambda *args: self.fail('status must not call bank')
        first = self.status(task['task_id'])
        self.assertEqual(first, self.status(task['task_id']))
        self.assertEqual(self.bank.state(), before)
        with closing(sqlite3.connect(self.tasks.path)) as db:
            document = json.loads(db.execute('SELECT document FROM tasks').fetchone()[0])
        self.assertNotIn('replan_guidance', document)

    def test_pause_handoff_can_replan_without_changing_existing_behavior(self):
        for action in ['pause', 'handoff']:
            with self.subTest(action=action):
                task = self.submit(key=action)
                stopped = (self.tasks.pause(task['task_id']) if action == 'pause'
                           else self.tasks.handoff(task['task_id'], '人工核对'))
                self.assertTrue(stopped['replan_guidance']['current_auto_advance_paused'])
                self.assertEqual(self.tasks.tick(task['task_id'])['state'], stopped['state'])
                revised = self.replan(stopped)
                self.assertEqual(revised['state'], 'READY')
                self.assertFalse(revised['replan_guidance']['current_auto_advance_paused'])

    def test_terminal_and_plan_version_guards_stay_enforced(self):
        task = self.submit()
        for _ in range(9): task = self.replan(task)
        self.assertEqual(task['plan_version'], 10)
        self.error('INVALID_PLAN', lambda: self.replan(task))
        cancelled = self.tasks.cancel(task['task_id'])
        self.error('INVALID_STATE', lambda: self.replan(cancelled))
        other = self.tasks.submit('只读', {'title': '只读', 'nodes': [node('balance')]}, 'other')
        done = self.tasks.tick(other['task_id'])
        self.error('INVALID_STATE', lambda: self.tasks.replan(done['task_id'], 1, [node('balance')], '不能追加'))

    def test_version_conflict_and_successful_node_immutability(self):
        task = self.submit(); task = self.tasks.tick(task['task_id'])
        changed = self.replan(task)
        self.error('VERSION_CONFLICT', lambda: self.replan(task))
        invalid = [node('balance', 'arrangements'), transfer()]
        self.error('IMMUTABLE_SUCCESS', lambda: self.replan(changed, nodes=invalid))
        self.assertEqual(self.status(changed['task_id']), changed)

    def test_waiting_card_is_conditional_and_change_requires_rejection(self):
        task = self.submit(); self.tasks.tick(task['task_id'])
        task = self.tasks.tick(task['task_id'])
        g = task['replan_guidance']
        self.assertEqual(g['eligibility'], 'CONDITIONAL')
        self.assertEqual(g['operation_checks_on_changed_nodes'][0]['operation_id'], task['nodes'][1]['operation_id'])
        self.error('PENDING_OPERATION', lambda: self.replan(task, 40000))
        # Unchanged waiting node is not globally forbidden from being preserved.
        same = self.replan(task)
        self.assertEqual(same['nodes'][1]['operation_id'], task['nodes'][1]['operation_id'])
        self.bank.user_action('reject', {'operation_id': task['nodes'][1]['operation_id']})
        revised = self.replan(same, 40000)
        self.assertEqual(revised['state'], 'READY')

    def test_expired_pending_can_change_but_external_success_cannot(self):
        task = self.submit(); self.tasks.tick(task['task_id']); task = self.tasks.tick(task['task_id'])
        self.now += timedelta(minutes=6)
        revised = self.replan(task, 40000)
        self.assertEqual(revised['plan_version'], 2)
        waiting = self.tasks.tick(task['task_id'])
        op = waiting['nodes'][1]['result']['data']['operation']
        self.bank.user_action('confirm', {'operation_id': op['operation_id'], 'expected_summary': op['summary']})
        # Snapshot is intentionally stale until tick; authoritative replan reads bank.
        self.assertEqual(self.status(task['task_id'])['replan_guidance']['eligibility'], 'CONDITIONAL')
        self.error('PENDING_OPERATION', lambda: self.replan(waiting, 30000))
        self.assertEqual(self.tasks.tick(task['task_id'])['state'], 'SUCCEEDED')

    def test_unknown_intent_requires_reconciliation_not_blanket_failed_permission(self):
        task = self.submit(); self.tasks.tick(task['task_id'])
        original = self.tasks.call_tool
        def uncertain(name, args):
            original(name, args)
            raise TimeoutError('simulated unknown result')
        self.tasks.call_tool = uncertain
        failed = self.tasks.tick(task['task_id'])
        self.assertEqual(failed['replan_guidance']['reconciliation_on_changed_node_ids'], ['pay'])
        self.error('RECONCILIATION_REQUIRED', lambda: self.replan(failed, 40000))
        self.tasks.call_tool = original
        self.tasks.retry(task['task_id'], 'pay')
        restored = self.tasks.tick(task['task_id'])
        self.assertEqual(restored['state'], 'WAITING_CONFIRMATION')
        self.assertEqual(restored['replan_guidance']['reconciliation_on_changed_node_ids'], [])
        self.assertEqual(len(self.bank.state()['operations']), 1)

    def test_retry_limit_does_not_become_replan_limit(self):
        task = self.submit(900000)
        self.tasks.tick(task['task_id'])
        failed = self.tasks.tick(task['task_id'])
        for _ in range(2):
            self.tasks.retry(task['task_id'], 'pay')
            failed = self.tasks.tick(task['task_id'])
        self.error('RETRY_LIMIT', lambda: self.tasks.retry(task['task_id'], 'pay'))
        self.assertEqual(failed['replan_guidance']['eligibility'], 'CONDITIONAL')
        self.assertEqual(self.replan(failed)['plan_version'], 2)

    def test_invalid_dag_still_rejected_despite_conditional_guidance(self):
        task = self.submit()
        self.assertEqual(task['replan_guidance']['eligibility'], 'CONDITIONAL')
        with self.assertRaises(TaskError): self.replan(task, nodes=[node('cycle', parents=['cycle'])])
        self.assertEqual(self.status(task['task_id']), task)

    def test_guidance_survives_restart_without_new_persisted_fields(self):
        task = self.submit(900000); self.tasks.tick(task['task_id']); failed = self.tasks.tick(task['task_id'])
        restarted = CompetitionService(self.root, bank=self.bank, clock=lambda: self.now)
        self.assertEqual(restarted.tasks.get(task['task_id']), failed)

    def test_tool_schema_and_persona_state_contract(self):
        descriptions = {s['name']: s['description'] for s in task_specs()}
        self.assertIn('replan_guidance', descriptions['task_status'])
        self.assertIn('FAILED', descriptions['replan_task'])
        self.assertIn('SUCCEEDED/CANCELLED', descriptions['replan_task'])
        persona = (Path(__file__).resolve().parents[2] / 'plugins/bank-v1/persona.txt').read_text(encoding='utf-8')
        self.assertIn('FAILED仍可按条件修复', persona)
        self.assertNotIn('未结束才修改未完成节点，已结束不能replan', persona)


if __name__ == '__main__': unittest.main(verbosity=2)
