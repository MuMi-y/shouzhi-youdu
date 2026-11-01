"""Read prohibition/date separation and account-scoped report delivery; no AI."""
from copy import deepcopy
from contextlib import closing
from datetime import datetime, timezone
import sqlite3
import unittest

from src.bank_requests import billing_dates, normalization, cashflow_dates
from src.bank_requests.actions import normalize
from src.bank_requests.context import build
from src.bank_requests.response import validate
from src.bank_v1_web.display_check import check
from tests.competition_ai.test_request_decision import item, slot, response
from tests.competition_ai import test_service_answer as fixtures


class ReadonlyDateScopeTests(unittest.TestCase):
    def test_complete_write_restrictions_do_not_negate_billing_period(self):
        for suffix in ['不创建计划或办理任何操作', '不改计划', '不需要创建任何目标或执行交易',
                       '不要准备转账', '不要付款或转账只做比较', '不转账']:
            prompt = '查询2026年9月账单，' + suffix + '。'
            with self.subTest(prompt=prompt):
                self.assertTrue(billing_dates.explicit_source('period_text', '2026年9月', prompt))

    def test_date_negation_alternatives_and_unknown_prohibitions_stay_unresolved(self):
        for prompt in ['不要查询2026年9月账单', '不是2026年9月账单', '查询2026年9月或2026年10月账单',
                       '大概2026年9月账单', '查2026年9月账单，不创建计划或查询上月',
                       '查2026年9月账单，不知道时间是否正确']:
            with self.subTest(prompt=prompt):
                self.assertFalse(billing_dates.explicit_source('period_text', '2026年9月', prompt))

    def test_goal_and_cashflow_sources_keep_actual_date_uncertainty(self):
        self.assertTrue(normalization.date_source_is_explicit('11月20日', '不要付款只分析11月20日买电脑'))
        self.assertFalse(normalization.date_source_is_explicit('11月20日', '不要11月20日买电脑'))
        text = '截至11月30日不要付款或转账只做比较'
        found = cashflow_dates.discoveries(text, [(0, len(text))])
        self.assertFalse(found['through_date_text'][0]['uncertain'])
        text = '截至11月30日或12月1日'
        self.assertTrue(cashflow_dates.discoveries(text, [(0, len(text))])['through_date_text'][0]['uncertain'])

    def test_model_prepare_label_cannot_override_current_prohibition(self):
        prompt = '从B到A转173.00元，只分析，不要准备转账。'
        ctx = build('m', 'm', [{'message_id': 'm', 'text': prompt}], [], read_only=False)
        with self.assertRaisesRegex(ValueError, 'EXPLICIT_WRITE_PROHIBITION'):
            normalize({'kind': 'PREPARE', 'source': {'message_id': 'm', 'quote': '从B到A转173.00元'}}, ctx)
        self.assertEqual('ANALYZE', normalize({'kind': 'ANALYZE', 'source': {'message_id': 'm', 'quote': prompt}}, ctx)['effective_kind'])
        affirmative = '请准备从B到A转173.00元。'
        ctx = build('m', 'm', [{'message_id': 'm', 'text': affirmative}], [], read_only=False)
        self.assertEqual('PREPARE', normalize({'kind': 'PREPARE', 'source': {'message_id': 'm', 'quote': affirmative}}, ctx)['effective_kind'])


class BillingAccountScopeTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ServiceAnswerTests(); self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        self.initial = {}
        for account in ['A', 'B', None]:
            args = {'start_date':'2026-09-01','end_date':'2026-09-30','compare_previous':False}
            if account:
                args['account_id'] = account
            self.initial[account] = self.f.service.call_tool('billing_report',args)['data']['report']['totals']['expense_minor']
        with closing(sqlite3.connect(self.f.service.bank.db_path)) as db:
            for identifier, account, amount in [('scope-a', 'A', -1111), ('scope-b', 'B', -7777)]:
                db.execute('INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?,?,?)',
                    (identifier, account, 'expense', amount, 'scope-fixture-'+account, '2026-09-15',
                     '2026-09-15T00:00:00+00:00', None, None, 'SYNTHETIC'))
            db.commit()

    def bill(self, account=None):
        args = {'start_date': '2026-09-01', 'end_date': '2026-09-30', 'compare_previous': False}
        if account is not None:
            args['account_id'] = account
        return self.f.tool('billing_report', args)

    def deliver(self, account='A', report_account='A', omit=False, prompt_override=None):
        self.f.tool('accounts', {})
        body = self.bill(report_account)
        prompt = f'查询账户{account}的2026年9月账单，只查询，不改计划。' if account else '查询2026年9月账单，只查询，不改计划。'
        prompt = prompt_override or prompt
        slots = {'period_text': slot('m', '2026年9月', '2026年9月')}
        if account and not omit:
            quote = '日常账户' if account == 'A' and '日常账户' in prompt else '账户'+account
            slots['source_account'] = slot('m', quote, account)
        req = item(slots, intent='billing', identifier='bill')
        req['action'] = {'kind': 'ANALYZE', 'source': {'message_id': 'm', 'quote': prompt}}
        ctx = build('m', 'm', [{'message_id': 'm', 'text': prompt}], [], read_only=False, object_contract=True)
        part = {'format': 'bank_billing_v1', 'report_evidence_id': body['evidence_id'], 'sections': ['overview'], 'fact_ids': [], 'calculation_ids': []}
        raw = response(ctx, [req], {'format': 'bank_outcomes_v1', 'items': [{'item_id': 'bill', 'components': [part]}]})
        raw['trace'] = deepcopy(self.f.trace)
        return validate(raw, ctx)

    def test_filter_fact_identity_and_default_total(self):
        a, b, combined = self.bill('A'), self.bill('B'), self.bill()
        self.assertEqual(self.initial['A']+1111, a['data']['report']['totals']['expense_minor'])
        self.assertEqual(self.initial['B']+7777, b['data']['report']['totals']['expense_minor'])
        self.assertEqual(self.initial[None]+8888, combined['data']['report']['totals']['expense_minor'])
        for body, entity in [(a, 'A'), (b, 'B'), (combined, 'COMBINED_ACCOUNT_LEDGER')]:
            facts = body['fact_catalog']['facts']
            self.assertTrue(facts)
            self.assertEqual({entity}, {fact['entity'] for fact in facts})
            if entity in {'A', 'B'}:
                self.assertTrue(all(f['period']['account_id'] == entity and f['source_arguments']['account_id'] == entity for f in facts))

    def test_single_account_full_response_and_display_recheck(self):
        result = self.deliver()
        self.assertTrue(result['ok'], result)
        self.assertIn('（A）', result['text'])
        self.assertTrue(check(result, self.f.service)['ok'])

    def test_exact_recorded_wording_with_correct_account_and_report(self):
        self.f.now = datetime(2026, 11, 1, 2, tzinfo=timezone.utc)
        self.f.tool('accounts', {})
        prompt = '请查询日常账户2026年10月1日至2026年10月31日的收入和支出汇总。只查询和分析，不创建计划或办理任何操作。'
        period = '2026年10月1日至2026年10月31日'
        slots = {'source_account': slot('m','日常账户','A'), 'period_text':slot('m',period,period),
                 'start_date':slot('m',period,'2026-10-01'),'end_date':slot('m',period,'2026-10-31')}
        body = self.f.tool('billing_report',{'start_date':'2026-10-01','end_date':'2026-10-31','compare_previous':False,'account_id':'A'})
        req = item(slots,intent='billing',identifier='bill')
        req['action'] = {'kind':'ANALYZE','source':{'message_id':'m','quote':prompt}}
        ctx = build('m','m',[{'message_id':'m','text':prompt}],[],read_only=False,object_contract=True)
        part={'format':'bank_billing_v1','report_evidence_id':body['evidence_id'],'sections':['overview'],'fact_ids':[],'calculation_ids':[]}
        raw=response(ctx,[req],{'format':'bank_outcomes_v1','items':[{'item_id':'bill','components':[part]}]})
        raw['trace']=deepcopy(self.f.trace)
        result=validate(raw,ctx)
        self.assertTrue(result['ok'],result.get('answer_validation'))
        self.assertIn('日常账户（A）',result['text'])
        self.assertTrue(check(result,self.f.service)['ok'])

    def test_combined_report_cannot_satisfy_single_account(self):
        result = self.deliver(report_account=None)
        self.assertFalse(result['ok'])
        self.assertEqual('BILLING_ACCOUNT_DOES_NOT_MATCH_REQUEST', result['answer_validation']['reason'])

    def test_explicit_account_cannot_be_omitted(self):
        result = self.deliver(report_account=None, omit=True)
        self.assertFalse(result['ok'])
        self.assertEqual('BILLING_ACCOUNT_SCOPE_OMITTED', result['answer_validation']['reason'])

    def test_default_combined_delivery_stays_compatible(self):
        result = self.deliver(account=None, report_account=None)
        self.assertTrue(result['ok'], result)
        self.assertIn('全部已授权账户合计', result['text'])

    def test_prohibited_other_account_is_not_a_second_query(self):
        result = self.deliver(prompt_override='仅查日常账户2026年9月账单，不要动储备账户。')
        self.assertTrue(result['ok'], result.get('answer_validation'))
        self.assertIn('日常账户（A）', result['text'])
        self.assertTrue(check(result, self.f.service)['ok'])
        rejected = self.deliver(prompt_override='查日常账户2026年9月账单或查储备账户。')
        self.assertFalse(rejected['ok'])

    def test_unknown_account_never_expands_to_total(self):
        for value in ['X', 'UNKNOWN', '', None, 1]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                self.f.service.insights.call_tool('billing_report', {'start_date':'2026-09-01','end_date':'2026-09-30','account_id':value})


if __name__ == '__main__':
    unittest.main()
