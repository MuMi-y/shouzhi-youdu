"""Historical report observations never serve as receipts for bank actions."""
import copy
import unittest

from src.bank_v1_web import bank_answer as gate


def report_record(net=-3725):
    return {'kind': 'response', 'ok': True, 'tool': 'billing_report',
            'response': {'scope': 'bank', 'currency': 'CNY', 'as_of': '2026-04-20',
                         'evidence_id': 'insight_fixture', 'data': {'report': {
        'report_contract_version': 2, 'currency': 'CNY', 'start_date': '2026-04-01', 'end_date': '2026-04-20',
        'totals': {'count': 2, 'net_change_minor': net, 'evidence_refs': ['tx_a', 'tx_b']}}}}}


def wording(net=-3725, direction=None):
    direction = direction or ('减少' if net < 0 else '增加' if net > 0 else '变化')
    return f'本期无内部转账，净流入为{net / 100:.2f}元，即该期间已入账资金的净{direction}'


def evaluate(text, records):
    return gate.validate_bank_answer({'ok': True, 'text': text, 'trace': records}, '')


class HistoricalPostedReportTests(unittest.TestCase):
    def assert_rejected(self, text, records, code=None):
        result = evaluate(text, records)
        self.assertFalse(result['ok'], result)
        if code:
            self.assertIn(code, {reason['code'] for reason in result['answer_validation']['reasons']})

    def test_signed_period_change_directions(self):
        for net in (-3725, -8900, 2468, 0):
            with self.subTest(net=net):
                self.assertTrue(evaluate(wording(net), [report_record(net)])['ok'])

    def test_period_subject_variants_are_local(self):
        for subject in ('本期', '该期', '该期间', '本观察期间', '该观察期间'):
            text = wording().replace('该期间', subject)
            self.assertTrue(evaluate(text, [report_record()])['ok'], text)

    def test_no_report_does_not_prove_history(self):
        self.assert_rejected(wording(), [], 'UNBACKED_OPERATION_SUCCESS')
        self.assert_rejected(wording().replace('本期无内部转账，', ''), [], 'UNBACKED_OPERATION_SUCCESS')

    def test_failed_foreign_missing_scope_and_wrong_tool(self):
        for changed in ('failed', 'foreign', 'missing_scope', 'wrong_tool'):
            record = report_record()
            if changed == 'failed': record['ok'] = False
            elif changed == 'foreign': record['response']['scope'] = 'moneydata'
            elif changed == 'missing_scope': record['response'].pop('scope')
            else: record['tool'] = 'accounts'
            with self.subTest(changed=changed):
                self.assert_rejected(wording(), [record], 'UNBACKED_OPERATION_SUCCESS')

    def test_only_direct_typed_report_path(self):
        for key in ('args', 'inputs', 'description', 'plan_history'):
            record = report_record()
            payload = record['response']['data'].pop('report')
            record['response']['data'][key] = {'report': payload} if key != 'description' else wording()
            with self.subTest(key=key):
                self.assert_rejected(wording(), [record], 'UNBACKED_OPERATION_SUCCESS')

    def test_invalid_typed_facts(self):
        for field, value in (('net_change_minor', '-3725'), ('net_change_minor', True),
                             ('count', True), ('count', -1)):
            record = report_record(); record['response']['data']['report']['totals'][field] = value
            self.assert_rejected(wording(), [record], 'UNBACKED_OPERATION_SUCCESS')

    def test_wrong_report_currency_and_version(self):
        for key, value in (('currency', 'USD'), ('report_contract_version', 1)):
            record = report_record(); record['response']['data']['report'][key] = value
            self.assert_rejected(wording(), [record], 'UNBACKED_OPERATION_SUCCESS')

    def test_old_other_kind_receipt_does_not_prove_extra_transfer(self):
        operation = {'kind':'response', 'ok':True, 'tool':'operation', 'response':{'scope':'bank', 'data':{
            'operation': {'operation_id':'op_old', 'kind':'mandate_query', 'status':'SUCCEEDED', 'receipt_id':'r_old'}}}}
        self.assert_rejected(wording() + '，转账已成功', [report_record(), operation], 'UNBACKED_OPERATION_SUCCESS')

    def test_root_reference_list_cannot_replace_typed_total_references(self):
        record = report_record(); report = record['response']['data']['report']
        report['evidence_refs'] = report['totals'].pop('evidence_refs')
        self.assert_rejected(wording(), [record], 'UNBACKED_OPERATION_SUCCESS')

    def test_missing_source_locator_and_references(self):
        for change in ('id', 'refs', 'string_refs', 'empty_ref'):
            record = report_record()
            if change == 'id': record['response']['evidence_id'] = ''
            else: record['response']['data']['report']['totals']['evidence_refs'] = {
                'refs': [], 'string_refs': 'tx_a', 'empty_ref': ['']}[change]
            self.assert_rejected(wording(), [record], 'UNBACKED_OPERATION_SUCCESS')

    def test_bad_or_future_period(self):
        for start, end in (('2026-04-01', '2026-04-21'), ('2026-04-30', '2026-04-01'),
                           ('2026-02-30', '2026-04-20')):
            record = report_record(); report = record['response']['data']['report']
            report.update(start_date=start, end_date=end)
            self.assert_rejected(wording(), [record], 'UNBACKED_OPERATION_SUCCESS')

    def test_wrong_explicit_iso_period(self):
        self.assert_rejected('2026-03-01至2026-03-20，' + wording(), [report_record()], 'UNBACKED_OPERATION_SUCCESS')
        self.assert_rejected('2026-04-10至2026-04-15，' + wording(), [report_record()], 'UNBACKED_OPERATION_SUCCESS')
        self.assertTrue(evaluate('2026-04-01至2026-04-20，' + wording(), [report_record()])['ok'])

    def test_ambiguous_period_and_net_values(self):
        for changed in ('period', 'net'):
            second = report_record()
            if changed == 'period': second['response']['data']['report']['start_date'] = '2026-04-02'
            else: second['response']['data']['report']['totals']['net_change_minor'] = -1200
            self.assert_rejected(wording(), [report_record(), second], 'UNBACKED_OPERATION_SUCCESS')

    def test_duplicate_same_report_is_not_ambiguity(self):
        record = report_record()
        self.assertTrue(evaluate(wording(), [record, copy.deepcopy(record)])['ok'])

    def test_direction_cannot_borrow_same_magnitude(self):
        self.assert_rejected(wording(direction='增加'), [report_record()], 'UNBACKED_OPERATION_SUCCESS')
        self.assert_rejected(wording(3725), [report_record()], 'UNBACKED_OPERATION_SUCCESS')

    def test_numeric_gate_still_checks_original_text(self):
        self.assert_rejected(wording() + '；余额为999.99元。', [report_record()], 'UNSUPPORTED_DISPLAYED_AMOUNT')
        self.assert_rejected(wording() + '；涨幅99%。', [report_record()], 'UNSUPPORTED_DISPLAYED_PERCENT')

    def test_history_without_net_value_has_no_new_permission(self):
        self.assert_rejected('无内部转账，即该期间已入账资金的净减少', [report_record()], 'UNBACKED_OPERATION_SUCCESS')

    def test_unknown_operation_id_is_not_erased(self):
        self.assert_rejected(wording() + ' op_unknown', [report_record()], 'UNBACKED_OPERATION_SUCCESS')

    def test_extra_bank_assertions_remain_checked(self):
        for claim in ('转账已成功', '代扣授权已撤销', '转账已入账', '已准备转账，请确认'):
            for separator in ('，', '；', '。', '并且'):
                with self.subTest(claim=claim, separator=separator):
                    self.assert_rejected(wording() + separator + claim, [report_record()])

    def test_same_clause_unknown_posting_is_not_report_observation(self):
        self.assert_rejected(wording() + '，收入已入账', [report_record()], 'UNBACKED_OPERATION_SUCCESS')

    def test_local_denial_cannot_hide_separate_positive(self):
        self.assert_rejected('没有内部转账，' + wording() + '，转账已成功', [report_record()], 'UNBACKED_OPERATION_SUCCESS')

    def test_preceding_sentence_dates_do_not_change_current_scope(self):
        self.assertTrue(evaluate('其他日期2026-03-01至2026-03-20。' + wording(), [report_record()])['ok'])

    def test_actual_posting_is_not_historical_attribute(self):
        for text in ('转账已入账', '本期转账已入账', '收入已入账，转账已成功'):
            self.assert_rejected(text, [report_record()], 'UNBACKED_OPERATION_SUCCESS')

    def test_old_unrelated_receipt_does_not_supply_missing_report(self):
        record = {'kind':'response', 'ok':True, 'tool':'operation', 'response':{'scope':'bank', 'data':{
            'operation': {'operation_id':'op_old', 'kind':'transfer', 'status':'SUCCEEDED', 'receipt_id':'r_old'}}}}
        self.assert_rejected(wording(), [record], 'UNBACKED_OPERATION_SUCCESS')

    def test_empty_observation_only_supports_zero(self):
        record = report_record(0); report = record['response']['data']['report']
        report['totals']['count'] = 0; report['totals']['evidence_refs'] = []
        self.assertTrue(evaluate(wording(0), [record])['ok'])
        report['totals']['net_change_minor'] = -3725
        self.assert_rejected(wording(), [record], 'UNBACKED_OPERATION_SUCCESS')

    def test_legacy_action_call_is_compatible(self):
        self.assertEqual([], gate.action_reasons('请核对账单日期', {}, {}))
        self.assertTrue(gate.action_reasons('转账已成功', {}, {}))


def receipt_record(kind='transfer', identifier='op_receipt', status='SUCCEEDED'):
    return {'kind': 'response', 'ok': True, 'tool': 'operation', 'response': {'scope': 'bank', 'data': {
        'operation': {'operation_id': identifier, 'kind': kind, 'status': status,
                      'receipt': {'receipt_id': 'receipt_test', 'operation_id': identifier,
                                  'kind': kind, 'status': 'SUCCEEDED'}}}}}


class ReceiptReadEvidenceTests(unittest.TestCase):
    def test_four_original_independent_failures_are_rejected(self):
        history = '本期无收入入账，无内部转账，净流入为-61.49元，即该期间已入账资金的净减少'
        record = report_record(-6149)
        record['response']['as_of'] = '2026-06-30'
        record['response']['data']['report'].update(start_date='2026-06-01', end_date='2026-06-30')
        for text in [history + separator + '已核对真实回执' for separator in ('，', '；', '。')] + [
                '没有内部转账，' + history + '，已核对真实回执']:
            with self.subTest(text=text):
                self.assertFalse(evaluate(text, [record])['ok'])

    def test_standalone_receipt_read_needs_evidence(self):
        for verb in ('读取', '核对'):
            for label in ('', '真实', '银行'):
                self.assertFalse(evaluate(f'已{verb}{label}回执', [])['ok'])

    def test_successful_receipt_supports_local_claim(self):
        for text in ('已核对真实回执', '已读取银行回执', '转账已读取回执',
                     '操作op_receipt已核对真实回执'):
            self.assertTrue(evaluate(text, [receipt_record()])['ok'], text)

    def test_correct_receipt_next_to_history(self):
        for separator in ('，', '；', '。'):
            self.assertTrue(evaluate(wording() + separator + '已核对真实回执',
                                     [report_record(), receipt_record()])['ok'])

    def test_question_condition_and_quoted_label_keep_semantics(self):
        for text in ('是否已核对真实回执？', '确认后再查回执。', '如果已核对真实回执，请告知。',
                     '尚未核对真实回执。', '界面标签「已读取银行回执」。', '标题“已核对真实回执”。'):
            self.assertTrue(evaluate(text, [])['ok'], text)

    def test_condition_does_not_hide_next_receipt_claim(self):
        for text in ('如果需要核对，请告知；已核对真实回执',
                     '是否已核对真实回执？已读取银行回执',
                     '没有转账，已核对真实回执'):
            self.assertFalse(evaluate(text, [])['ok'], text)

    def test_pending_failed_or_empty_receipt_is_insufficient(self):
        for status in ('PENDING', 'FAILED', 'REJECTED', 'UNKNOWN'):
            self.assertFalse(evaluate('已核对真实回执', [receipt_record(status=status)])['ok'])
        for receipt in (None, {}, 'receipt_test', {'receipt_id': ''}, {'receipt_id': True}):
            record = receipt_record(); record['response']['data']['operation']['receipt'] = receipt
            self.assertFalse(evaluate('已核对真实回执', [record])['ok'])

    def test_failed_or_foreign_tool_response_cannot_prove_read(self):
        for change in ('failed', 'foreign', 'wrong_tool'):
            record = receipt_record()
            if change == 'failed': record['ok'] = False
            elif change == 'foreign': record['response']['scope'] = 'moneydata'
            else: record['tool'] = 'billing_report'
            self.assertFalse(evaluate('已核对真实回执', [record])['ok'])

    def test_missing_scope_legacy_bank_envelope_is_compatible(self):
        record = receipt_record(); record['response'].pop('scope')
        self.assertTrue(evaluate('已核对真实回执', [record])['ok'])

    def test_nested_receipt_fields_must_match_operation(self):
        for key, value in (('operation_id', 'op_other'), ('kind', 'mandate_query'), ('status', 'PENDING')):
            record = receipt_record(); record['response']['data']['operation']['receipt'][key] = value
            self.assertFalse(evaluate('已核对真实回执', [record])['ok'])

    def test_legacy_receipt_identifier_is_supported(self):
        record = receipt_record(); op = record['response']['data']['operation']
        op.pop('receipt'); op['receipt_id'] = 'receipt_test'
        self.assertTrue(evaluate('已读取回执', [record])['ok'])

    def test_description_and_echoed_arguments_do_not_prove_read(self):
        for key in ('args', 'inputs', 'description', 'plan_history'):
            record = receipt_record(); operation = record['response']['data'].pop('operation')
            record['response']['data'][key] = {'operation': operation}
            self.assertFalse(evaluate('已核对真实回执', [record])['ok'])

    def test_explicit_kind_and_id_must_match(self):
        for text in ('撤销操作已核对真实回执', '操作op_unknown已读取银行回执'):
            self.assertFalse(evaluate(text, [receipt_record()])['ok'])
        self.assertTrue(evaluate('转账op_receipt已核对真实回执', [receipt_record()])['ok'])

    def test_successful_task_node_receipt_is_usable(self):
        envelope = receipt_record()['response']
        record = {'kind': 'response', 'ok': True, 'tool': 'task_status', 'response': {'scope': 'bank', 'data': {
            'task': {'task_id': 'a' * 32, 'scope': 'bank', 'subject_id': 'U1', 'state': 'SUCCEEDED',
                     'nodes': [{'id': 'read', 'tool': 'operation', 'state': 'SUCCEEDED', 'result': envelope}]}}}}
        self.assertTrue(evaluate('已核对真实回执', [record])['ok'])
        for state in ('PENDING', 'FAILED', 'WAITING_CONFIRMATION'):
            changed = copy.deepcopy(record)
            changed['response']['data']['task']['nodes'][0]['state'] = state
            self.assertFalse(evaluate('已核对真实回执', [changed])['ok'])

    def test_task_scope_subject_and_nested_tool_are_checked(self):
        task = {'task_id': 'a' * 32, 'scope': 'bank', 'subject_id': 'U1', 'state': 'SUCCEEDED',
                'nodes': [{'id': 'read', 'tool': 'operation', 'state': 'SUCCEEDED', 'result': receipt_record()['response']}]}
        for change in ('scope', 'subject', 'node_scope', 'node_tool'):
            changed = copy.deepcopy(task)
            if change == 'scope': changed['scope'] = 'moneydata'
            elif change == 'subject': changed['subject_id'] = 'U2'
            elif change == 'node_scope': changed['nodes'][0]['result']['scope'] = 'moneydata'
            else: changed['nodes'][0]['tool'] = 'billing_report'
            record = {'kind': 'response', 'ok': True, 'tool': 'task_status',
                      'response': {'scope': 'bank', 'data': {'task': changed}}}
            self.assertFalse(evaluate('已核对真实回执', [record])['ok'])

    def test_quoted_label_cannot_hide_unquoted_assertion(self):
        self.assertFalse(evaluate('标签「已核对真实回执」，已读取银行回执', [])['ok'])


if __name__ == '__main__':
    unittest.main()
