"""Shared condition with real isolated bank transitions, never a model substitute."""
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path
import tempfile
import unittest

from src.bank_v1.balance_conditions import assess_balance
from src.bank_v1.common import DomainError
from src.bank_v1.service import Service


class BalanceConditionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='balance-condition-')
        self.addCleanup(temporary.cleanup)
        self.bank = Service(Path(temporary.name) / 'bank.sqlite3',
                            clock=lambda: datetime(2026, 10, 19, 2, tzinfo=timezone.utc))

    def prepare(self, amount, key, source='B', target='A'):
        return self.bank.call_tool('prepare_transfer', {
            'source_account': source, 'target_account': target,
            'amount_minor': amount, 'idempotency_key': key})['data']['operation']

    def inject(self, kind, amount, key):
        self.bank.user_action('inject_transaction', {
            'kind': kind, 'amount_minor': amount, 'account_id': 'B',
            'description': '虚构条件变更', 'idempotency_key': key})

    def confirm(self, operation):
        return self.bank.user_action('confirm', {
            'operation_id': operation['operation_id'],
            'expected_summary': operation['summary']})

    def test_boundary_condition_is_immutable_and_does_not_claim_authorization(self):
        for balance, amount, expected in [(-1, 1, 'INSUFFICIENT_FUNDS'),
                (0, 1, 'INSUFFICIENT_FUNDS'), (1, 1, 'BALANCE_SUFFICIENT'),
                (2, 1, 'BALANCE_SUFFICIENT'), (1, 2, 'INSUFFICIENT_FUNDS')]:
            with self.subTest(balance=balance, amount=amount):
                result = assess_balance(balance, amount)
                self.assertEqual(expected, result.status)
                self.assertEqual({'balance_minor', 'proposed_minor', 'status'}, set(vars(result)))
                with self.assertRaises(FrozenInstanceError):
                    result.status = 'AUTHORIZED'
        for balance, amount in [(True, 1), (1, True), (1.0, 1), (1, 0), (1, -1), (1, '1')]:
            with self.assertRaises(ValueError):
                assess_balance(balance, amount)

    def test_preparation_agrees_with_condition_at_account_boundary(self):
        before = self.bank.state()['accounts']
        for amount in (99999, 100000, 100001):
            with self.subTest(amount=amount):
                condition = assess_balance(100000, amount)
                if condition.status == 'INSUFFICIENT_FUNDS':
                    with self.assertRaises(DomainError) as caught:
                        self.prepare(amount, 'boundary-' + str(amount))
                    self.assertEqual(condition.status, caught.exception.code)
                else:
                    operation = self.prepare(amount, 'boundary-' + str(amount))
                    self.assertEqual('PENDING', operation['status'])
                    self.assertIsNone(operation['receipt'])
        self.assertEqual(before, self.bank.state()['accounts'])

    def test_balance_change_between_prepare_and_confirm_requires_new_check(self):
        operation = self.prepare(80000, 'changing-balance')
        self.inject('expense', 30000, 'spend-before-confirm')
        before = self.bank.state()
        with self.assertRaises(DomainError) as caught:
            self.confirm(operation)
        self.assertEqual('INSUFFICIENT_FUNDS', caught.exception.code)
        self.assertEqual(before['accounts'], self.bank.state()['accounts'])
        current = self.bank.call_tool('operation', {'operation_id': operation['operation_id']})['data']['operation']
        self.assertEqual('PENDING', current['status'])
        self.assertIsNone(current['receipt'])
        self.inject('income', 20000, 'fund-original-operation')
        completed = self.confirm(operation)
        self.assertEqual('SUCCEEDED', completed['status'])
        self.assertEqual(80000, completed['receipt']['amount_minor'])
        after = self.bank.state()
        self.assertEqual(completed['receipt'], self.confirm(operation)['receipt'])
        self.assertEqual(after, self.bank.state())

    def test_funding_changes_preparation_condition_without_new_rule(self):
        with self.assertRaises(DomainError) as caught:
            self.prepare(100001, 'after-funding')
        self.assertEqual('INSUFFICIENT_FUNDS', caught.exception.code)
        self.inject('income', 1, 'one-cent-funding')
        operation = self.prepare(100001, 'after-funding')
        self.assertEqual('PENDING', operation['status'])
        self.assertEqual('STRONG', operation['required_verification'])
        with self.assertRaises(DomainError) as caught:
            self.confirm(operation)
        self.assertEqual('STRONG_VERIFICATION_REQUIRED', caught.exception.code)

    def test_sufficient_balance_cannot_bypass_identity_or_same_account_checks(self):
        self.assertEqual('BALANCE_SUFFICIENT', assess_balance(100000, 1).status)
        for source, target, expected in [('B', 'B', 'INVALID_ACCOUNT'),
                                          ('B', 'X', 'FORBIDDEN'), ('B', 'missing', 'NOT_FOUND')]:
            with self.subTest(source=source, target=target):
                with self.assertRaises(DomainError) as caught:
                    self.prepare(1, 'identity-' + target, source, target)
                self.assertEqual(expected, caught.exception.code)

    def test_existing_preparation_identity_is_preserved_after_balance_change(self):
        operation = self.prepare(80000, 'same-intent')
        self.inject('expense', 30000, 'later-expense')
        self.assertEqual(operation, self.prepare(80000, 'same-intent'))
        with self.assertRaises(DomainError) as caught:
            self.prepare(70000, 'same-intent')
        self.assertEqual('IDEMPOTENCY_CONFLICT', caught.exception.code)
