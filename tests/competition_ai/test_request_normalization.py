"""Source-backed aliases and calendar boundaries, no model or external calls."""
from copy import deepcopy
from datetime import datetime, timezone
import unittest
from tests.competition_ai import test_service_answer as fixtures
from tests.competition_ai.test_request_decision import item, slot
from src.bank_requests.context import build
from src.bank_requests.decision import validate
from src.bank_requests.components import records


class RequestNormalizationTests(unittest.TestCase):
    def setUp(self):
        self.f = fixtures.ServiceAnswerTests(); self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def normalized(self, body, quote, value, kind, **extra):
        result = slot('m', quote, value)
        result['source']['normalization'] = {'kind': kind, 'evidence_id': body['evidence_id'], **extra}
        return result

    def decide(self, text, slots, intent):
        context = build('m', 'm', [{'message_id': 'm', 'text': text}], [], read_only=False)
        return validate([item(slots, intent=intent)], context, records(self.f.trace))

    def test_actual_account_name_maps_to_code_and_wrong_choice_rejected(self):
        body = self.f.tool('accounts', {})
        account = body['data']['accounts'][0]
        chosen = self.normalized(body, account['name'], account['account_id'], 'account_name')
        decision = self.decide(account['name'], {'source_account': chosen}, 'account_overview')
        self.assertEqual(account['account_id'], decision['items'][0]['slots']['source_account']['value'])
        chosen['value'] = 'B' if account['account_id'] == 'A' else 'A'
        with self.assertRaisesRegex(ValueError, 'INCORRECT_NORMALIZED_VALUE'):
            self.decide(account['name'], {'source_account': chosen}, 'account_overview')

    def test_month_year_leap_and_business_timezone_boundaries(self):
        for now, expected in [(datetime(2026, 1, 1, 2, tzinfo=timezone.utc), ('2025-12-01', '2025-12-31')),
                              (datetime(2028, 3, 1, 2, tzinfo=timezone.utc), ('2028-02-01', '2028-02-29')),
                              (datetime(2026, 2, 28, 18, tzinfo=timezone.utc), ('2026-02-01', '2026-02-28'))]:
            with self.subTest(now=now):
                self.f.now = now; self.f.trace = []
                body = self.f.tool('accounts', {})
                slots = {field: self.normalized(body, '上月', value, 'calendar_period', period='PREVIOUS_MONTH')
                         for field, value in zip(('start_date', 'end_date'), expected)}
                decision = self.decide('查上月账单', slots, 'billing')
                self.assertEqual(list(expected), [s['value'] for s in decision['items'][0]['slots'].values()])

    def test_wrong_range_or_changed_business_date_is_rejected(self):
        body = self.f.tool('accounts', {})
        chosen = self.normalized(body, '近30天', '2026-09-19', 'calendar_period', period='PAST_30_DAYS')
        with self.assertRaisesRegex(ValueError, 'INCORRECT_NORMALIZED_VALUE'):
            self.decide('近30天', {'start_date': chosen}, 'billing')
        chosen['value'] = '2026-09-20'
        self.decide('近30天', {'start_date': chosen}, 'billing')
        self.f.trace[-1]['response']['business_date'] = '2026-10-20'
        with self.assertRaisesRegex(ValueError, 'INVALID_NORMALIZATION_SOURCE_HASH'):
            self.decide('近30天', {'start_date': chosen}, 'billing')

    def test_old_balance_snapshot_cannot_supply_new_alias_normalization(self):
        body = self.f.tool('accounts', {})
        account = body['data']['accounts'][0]
        chosen = self.normalized(body, account['name'], account['account_id'], 'account_name')
        self.f.service.bank.user_action('inject_transaction', {'kind': 'income', 'account_id': account['account_id'],
                'amount_minor': 100, 'description': '到账', 'idempotency_key': 'new'})
        self.f.tool('accounts', {})
        with self.assertRaisesRegex(ValueError, 'NORMALIZATION_SOURCE_STALE'):
            self.decide(account['name'], {'source_account': chosen}, 'account_overview')
