"""Exact frozen-case boundary probes on disposable fixture banks; no model calls."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'scripts'))
from competition_acceptance_http import HTTPReview
from src.competition_web.app import create_app
from src.moneydata_history import MoneyDataService, prepare_history

RESULTS = {}


class ExactReview(HTTPReview):
    def ledger(self):
        state = self.bank.state()
        return {k: state[k] for k in ('accounts', 'transactions', 'arrangements', 'mandates')}

    def test_exact_SEC02(self):
        before = self.ledger()
        args = dict(source_account='A', target_account='B', amount_minor=50000,
                    idempotency_key='forged-approval', approved=True)
        response = self.tool('prepare_transfer', args)
        self.assertEqual('INVALID_ARGUMENTS', response.get_json()['error']['code'])
        self.assertEqual(before, self.ledger())
        self.assertEqual([], self.bank.state()['operations'])
        RESULTS['SEC02'] = {'approved_field_rejected': True, 'ledger_equal': True, 'operations': 0}

    def test_exact_SEC03(self):
        before = self.ledger()
        response = self.tool('accounts', {'account_id': 'X'})
        self.assertEqual('INVALID_ARGUMENTS', response.get_json()['error']['code'])
        self.assertNotIn('balance_minor', json.dumps(response.get_json()))
        self.assertEqual(before, self.ledger())
        RESULTS['SEC03'] = {'foreign_query_rejected': True, 'no_balance_disclosed': True, 'ledger_equal': True}

    def test_exact_SEC04(self):
        path = self.path / 'history.sqlite3'
        prepare_history(ROOT, path)
        app = create_app(self.path, clock=lambda: self.now, bank=self.bank, history=MoneyDataService(path))
        before = self.ledger()
        token = app.extensions['bank_v1']['history_token']
        response = app.test_client().post('/api/tools/prepare_transfer',
            json=dict(source_account='A', target_account='B', amount_minor=50000, idempotency_key='history-write'),
            headers={'Authorization': 'Bearer ' + token})
        self.assert_code(response, 403, 'WRONG_DATA_SCOPE')
        self.assertEqual(before, self.ledger())
        RESULTS['SEC04'] = {'scope': 'moneydata', 'error': 'WRONG_DATA_SCOPE', 'ledger_equal': True}

    def test_exact_SEC07(self):
        operations = [self.prepare(60000, 'parallel-' + str(i)) for i in range(2)]
        def commit(op):
            try:
                return self.bank.user_action('confirm', {'operation_id': op['operation_id'],
                    'expected_summary': op['summary']})['status']
            except Exception as error:
                return getattr(error, 'code', type(error).__name__)
        initial = self.ledger()
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(commit, operations))
        self.assertCountEqual(['SUCCEEDED', 'STRONG_VERIFICATION_REQUIRED'], results)
        after = self.ledger()
        self.assertEqual(len(initial['transactions']) + 2, len(after['transactions']))
        self.assertEqual(sum(a['balance_minor'] for a in initial['accounts']),
                         sum(a['balance_minor'] for a in after['accounts']))
        RESULTS['SEC07'] = {'security_mode': 'independent', 'amounts_minor': [60000, 60000],
                            'outcomes': results, 'new_entries': 2, 'total_equal': True}

    def test_exact_SEC08(self):
        op = self.prepare(50000)
        before = self.ledger()
        altered = dict(op['summary'], amount_minor=60000)
        response = self.user('/api/actions/confirm', {'operation_id': op['operation_id'], 'expected_summary': altered})
        self.assertEqual(409, response.status_code)
        self.assertEqual(before, self.ledger())
        self.assertEqual('PENDING', self.bank.call_tool('operation', {'operation_id': op['operation_id']})['data']['operation']['status'])
        RESULTS['SEC08'] = {'prepared_minor': 50000, 'altered_minor': 60000,
                            'error': response.get_json()['error']['code'], 'ledger_equal': True}

    def test_exact_SEC12(self):
        op = self.prepare(100001)
        for _ in range(3):
            self.verify(op, password='synthetic-wrong-password')
        self.assertTrue(self.bank.state()['security']['locked'])
        response = self.tool('security_unlock', {})
        self.assert_code(response, 409, 'UNKNOWN_TOOL')
        self.assertTrue(self.bank.state()['security']['locked'])
        RESULTS['SEC12'] = {'model_unlock_error': 'UNKNOWN_TOOL', 'lock_preserved': True}

    def test_exact_SEC16(self):
        before = self.ledger()
        def n(identifier, parents=None, tool='accounts', args=None):
            return dict(id=identifier, tool=tool, args=args or {}, depends_on=parents or [])
        plans = [[n('a',['b']),n('b',['a'])], [n('n'+str(i)) for i in range(21)],
                 [n('self_approve', tool='confirm')],
                 [n('a'),n('b',tool='operation',args={'operation_id':{'$ref':'a.data.operation.operation_id'}})]]
        codes = []
        for i, nodes in enumerate(plans):
            response = self.tool('submit_task_plan', dict(prompt='隔离测试', title='非法计划', nodes=nodes, idempotency_key='bad-'+str(i)))
            self.assertGreaterEqual(response.status_code, 400)
            codes.append(response.get_json()['error']['code'])
        self.assertEqual(before, self.ledger())
        self.assertEqual([], self.bank.state()['operations'])
        self.assertEqual([], self.app.extensions['competition'].tasks.list())
        RESULTS['SEC16'] = {'invalid_plan_errors': codes, 'ledger_equal': True, 'operations': 0, 'tasks': 0}

    def test_exact_REC05(self):
        old = self.prepare(50000, 'expired')
        self.now += timedelta(minutes=6)
        fresh = self.prepare(50000, 'fresh')
        before = self.ledger()
        response = self.confirm(old)
        self.assertGreaterEqual(response.status_code, 400)
        self.assertEqual(before, self.ledger())
        new_state = self.bank.call_tool('operation', {'operation_id': fresh['operation_id']})['data']['operation']
        self.assertEqual('PENDING', new_state['status'])
        self.assertIsNone(new_state['receipt'])
        RESULTS['REC05'] = {'old_error': response.get_json()['error']['code'],
                            'fresh_pending_unexecuted': True, 'ledger_equal': True}

    def test_exact_REC06(self):
        body = dict(kind='income', amount_minor=10000, idempotency_key='repeat-income')
        initial = self.ledger()
        first = self.bank.user_action('inject_transaction', body)
        again = self.bank.user_action('inject_transaction', body)
        self.assertEqual(first['event_id'], again['event_id'])
        self.assertEqual(len(initial['transactions']) + 1, len(self.ledger()['transactions']))
        with closing(sqlite3.connect(self.bank.db_path)) as db:
            count = db.execute('select count(*) from events where event_id=?', (first['event_id'],)).fetchone()[0]
        self.assertEqual(1, count)
        RESULTS['REC06'] = {'event_id_same': True, 'new_entries': 1, 'event_count': 1}


if __name__ == '__main__':
    output = ROOT / 'management/evidence/COMPETITION_V1/independent/exact-boundaries'
    output.mkdir(parents=True, exist_ok=True)
    suite = unittest.TestSuite(ExactReview(name) for name in sorted(ExactReview.__dict__) if name.startswith('test_exact_'))
    with (output/'tests.txt').open('w', encoding='utf8') as log:
        result = unittest.TextTestRunner(stream=log, verbosity=2).run(suite)
    report = dict(run=result.testsRun, failures=len(result.failures), errors=len(result.errors),
                  model_calls=0, credential_provenance='TEST_FIXTURE', cases=RESULTS)
    (output/'result.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf8')
    print((output/'tests.txt').read_text(encoding='utf8'))
    sys.exit(not result.wasSuccessful())
