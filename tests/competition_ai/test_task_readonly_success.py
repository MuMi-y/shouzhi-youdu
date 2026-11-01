"""Only current, uniquely identified read-only node results prove success."""
import copy
import unittest
from src.bank_v1_web import bank_answer as gate

TASK = 'a' * 32


def node(tool='accounts', identifier='check_balance', data=None):
    if data is None:
        data = {'accounts': [], 'total_balance_minor': 0}
    return {'id': identifier, 'tool': tool, 'state': 'SUCCEEDED', 'attempt': 1, 'error': None,
            'operation_id': None, 'result': {'evidence_id': 'evidence_test', 'data': data}}


def task(nodes=None, identifier=TASK):
    return {'task_id': identifier, 'scope': 'bank', 'subject_id': 'U1', 'state': 'WAITING_CONFIRMATION',
            'nodes': [node()] if nodes is None else nodes}


def response(value=None, tool='task_status', ok=True):
    return {'kind': 'response', 'tool': tool, 'ok': ok, 'args': {'task_id': TASK},
            'response': {'scope': 'bank', 'data': {'task': task() if value is None else value}}}


def check(text='余额查询节点：保留原已成功结果', records=None):
    return gate.validate_bank_answer({'ok': True, 'text': text,
                                     'trace': [response()] if records is None else records}, '说明任务进度')


class TaskReadonlySuccessTests(unittest.TestCase):
    def test_current_accounts_result_without_result_scope(self):
        self.assertTrue(check()['ok'])

    def test_existing_readonly_result_families(self):
        cases = [('账户查询', 'accounts', {'accounts': [], 'total_balance_minor': 0}),
                 ('流水查询', 'transactions', {'transactions': []}),
                 ('支出统计', 'spending_summary', {'expense_minor': 0, 'income_minor': 0, 'refund_minor': 0}),
                 ('安排查询', 'arrangements', {'arrangements': []}),
                 ('现金流预测', 'forecast', {'through_date': '2026-10-30', 'daily': [], 'current_balance_minor': 0}),
                 ('账单分析', 'billing_report', {'report': {'totals': {'count': 0}, 'start_date': '2026-09-01', 'end_date': '2026-09-30'}}),
                 ('周期识别', 'recurring_candidates', {'candidates': []}),
                 ('关注状态查询', 'insight_status', {'candidates': [], 'notifications': []}),
                 ('金额计算', 'money_calculate', {'calculation': {'result': {'kind': 'amount', 'amount_minor': 0}}})]
        for label, tool, data in cases:
            with self.subTest(tool=tool):
                self.assertTrue(check(label + '节点已成功', [response(task([node(tool, data=data)]))])['ok'])

    def test_missing_trace_rejects_claim(self):
        self.assertFalse(check(records=[])['ok'])

    def test_unexecuted_and_failed_node_rejected(self):
        for state in ('PENDING', 'FAILED', 'RUNNING', 'WAITING_CONFIRMATION'):
            value = node(); value['state'] = state
            self.assertFalse(check(records=[response(task([value]))])['ok'])
        value = node(); value['attempt'] = 0
        self.assertFalse(check(records=[response(task([value]))])['ok'])

    def test_invalid_result_and_evidence_rejected(self):
        for result in (None, {}, {'evidence_id': 'x', 'data': {}},
                       {'evidence_id': 'x', 'data': {'description': 'accounts succeeded'}},
                       {'data': {'accounts': [], 'total_balance_minor': 0}},
                       {'evidence_id': 'x', 'scope': 'history', 'data': {'accounts': [], 'total_balance_minor': 0}}):
            value = node(); value['result'] = result
            self.assertFalse(check(records=[response(task([value]))])['ok'])

    def test_failed_latest_status_invalidates_prior_success(self):
        self.assertFalse(check(records=[response(), response(ok=False)])['ok'])
        foreign = response(); foreign['response']['scope'] = 'history'
        self.assertFalse(check(records=[response(), foreign])['ok'])

    def test_wrong_subject_or_task_scope(self):
        for field, wrong in (('scope', 'history'), ('subject_id', 'U2')):
            value = task(); value[field] = wrong
            self.assertFalse(check(records=[response(value)])['ok'])

    def test_explicit_task_id_must_match(self):
        self.assertTrue(check('任务 ' + TASK + '\n余额查询节点已成功')['ok'])
        self.assertFalse(check('任务 ' + 'b' * 32 + '\n余额查询节点已成功')['ok'])

    def test_multiple_tasks_without_locator_are_ambiguous(self):
        other = response(task(identifier='b' * 32)); other['args']['task_id'] = 'b' * 32
        self.assertFalse(check(records=[response(), other])['ok'])
        self.assertTrue(check('任务 ' + TASK + '\n余额查询节点已成功', [response(), other])['ok'])

    def test_explicit_node_id_must_match(self):
        self.assertTrue(check('余额查询节点（check_balance）已成功')['ok'])
        self.assertFalse(check('余额查询节点（unknown_node）已成功')['ok'])

    def test_same_tool_multiple_nodes_not_success_subset(self):
        other = node(identifier='second'); other['state'] = 'FAILED'
        records = [response(task([node(), other]))]
        self.assertFalse(check(records=records)['ok'])
        self.assertTrue(check('余额查询节点（check_balance）已成功', records)['ok'])
        self.assertFalse(check('余额查询节点（second）已成功', records)['ok'])

    def test_duplicate_node_ids_rejected(self):
        self.assertFalse(check(records=[response(task([node(), node()]))])['ok'])

    def test_history_and_args_do_not_supply_nodes(self):
        value = task([]); value['plan_history'] = [{'nodes': [node()]}]
        self.assertFalse(check(records=[response(value)])['ok'])
        value = task([]); value['args'] = {'nodes': [node()]}
        self.assertFalse(check(records=[response(value)])['ok'])
        self.assertFalse(check(records=[response(tool='replan_task')])['ok'])
        self.assertFalse(check(records=[response(tool='submit_task_plan')])['ok'])

    def test_known_label_cannot_claim_other_tool(self):
        for tool in ('prepare_transfer', 'operation', 'check_insights', 'unknown_tool'):
            value = node(tool=tool)
            self.assertFalse(check(records=[response(task([value]))])['ok'])

    def test_operation_node_is_not_readonly_success(self):
        value = node(); value['operation_id'] = 'op_other'
        self.assertFalse(check(records=[response(task([value]))])['ok'])

    def test_adjacent_bank_claims_stay_independent(self):
        for sep in ('，', '；', '。', '\n'):
            for claim in ('转账已成功', '代扣授权已撤销', '已核对真实回执', '已建立转账确认卡'):
                self.assertFalse(check('余额查询节点已成功' + sep + claim)['ok'])
                self.assertFalse(check(claim + sep + '余额查询节点已成功')['ok'])

    def test_task_not_completed_by_one_node(self):
        self.assertFalse(check('余额查询节点已成功；任务已全部完成')['ok'])

    def test_double_negation_requires_same_evidence(self):
        for text in ('余额查询节点并非未成功', '不是余额查询节点未成功'):
            self.assertTrue(check(text)['ok'])
            self.assertFalse(check(text, [])['ok'])

    def test_no_new_claim_for_conditional_or_quote(self):
        self.assertTrue(check('如果余额查询节点已成功，请查看原任务。', [])['ok'])
        self.assertTrue(check('标签“余额查询节点已成功”仅为说明', [])['ok'])

    def test_numbers_still_require_tool_values(self):
        result = check('余额查询节点已成功，余额871.23元')
        self.assertIn('UNSUPPORTED_DISPLAYED_AMOUNT', [r['code'] for r in result['answer_validation']['reasons']])


if __name__ == '__main__':
    unittest.main()
