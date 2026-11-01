"""Request revisions and explicit provenance; constructed decisions, not model quality."""
from copy import deepcopy
import json
import unittest

from src.bank_requests.context import build
from src.bank_requests.decision import validate, assumptions
from src.bank_requests.response import validate as validate_response
from src.bank_evidence.calculation import assumption_matches


def slot(message, quote, value):
    return {'value': value, 'source': {'message_id': message, 'quote': quote}}


def item(slots=None, missing=None, parent=None, conflicts=None, identifier='transfer', intent='transfer'):
    return {'id': identifier, 'intent': intent, 'continuation_of': parent,
            'slots': slots or {}, 'missing_fields': missing or [], 'conflicts': conflicts or []}


def response(context, items, answer):
    return {'ok': True, 'session_id': 'fixture-session', 'trace': [], 'text': json.dumps({
        'format': 'bank_response_v2', 'request_id': context['request_id'], 'items': items, 'answer': answer})}


class RequestDecisionTests(unittest.TestCase):
    def setUp(self):
        self.messages = [{'message_id': 'm1', 'text': '从A转到B'},
                         {'message_id': 'm2', 'text': '1280.50元'},
                         {'message_id': 'm3', 'text': '改为975.25元'}]
        self.first = item({'source_account': slot('m1', 'A', 'A'),
                           'target_account': slot('m1', 'B', 'B')}, ['amount_minor'])
        self.c1 = build('m1', 'm1', self.messages[:1], [], read_only=False)
        self.d1 = validate([self.first], self.c1)['items'][0]
        self.parent1 = {**self.d1, 'reference': 'm1:transfer'}
        self.c2 = build('m2', 'm2', self.messages[:2], [self.parent1], read_only=False)
        self.second = item({'amount_minor': slot('m2', '1280.50元', 128050)}, parent='m1:transfer')
        self.d2 = validate([self.second], self.c2)['items'][0]
        self.parent2 = {**self.d2, 'reference': 'm2:transfer'}
        self.c3 = build('m3', 'm3', self.messages, [self.parent1, self.parent2], read_only=False)

    def test_fill_then_revise_retains_accounts_and_excludes_old_amount(self):
        third = item({'amount_minor': slot('m3', '975.25元', 97525)}, parent='m2:transfer')
        decision = validate([third], self.c3)
        self.assertEqual({'source_account', 'target_account', 'amount_minor'}, set(decision['items'][0]['slots']))
        scope = assumptions(decision, self.c3)
        self.assertTrue(assumption_matches({'quote': '975.25元', 'value_minor': 97525}, scope))
        self.assertFalse(assumption_matches({'quote': '1280.50元', 'value_minor': 128050}, scope))

    def test_full_known_parameters_cannot_be_asked_again(self):
        wrong = deepcopy(self.second); wrong['missing_fields'] = ['amount_minor']
        with self.assertRaisesRegex(ValueError, 'KNOWN_OR_CONFLICTING'):
            validate([wrong], self.c2)
        guidance = {'format': 'bank_service_v1', 'parts': [{'format': 'bank_guidance_v1',
            'kind': 'request_parameters', 'tool': 'prepare_transfer', 'fields': ['amount_minor']}]}
        rejected = validate_response(response(self.c2, [self.second], guidance), self.c2)
        self.assertFalse(rejected['ok'])
        self.assertEqual('QUESTION_FOR_KNOWN_OR_UNDECLARED_FIELD', rejected['answer_validation']['reason'])

    def test_superseded_parent_and_new_item_borrowing_old_amount_are_rejected(self):
        with self.assertRaisesRegex(ValueError, 'SUPERSEDED_PARENT'):
            validate([item(parent='m1:transfer', missing=['amount_minor'])], self.c3)
        borrowed = item({'amount_minor': slot('m2', '1280.50元', 128050)}, ['source_account', 'target_account'])
        with self.assertRaisesRegex(ValueError, 'REVISION_REQUIRES_CURRENT'):
            validate([borrowed], self.c3)

    def test_unknown_message_quote_amount_and_ambiguous_amounts_rejected(self):
        for changed in [slot('assistant-summary', '1280.50元', 128050), slot('m2', '9000元', 900000),
                        slot('m2', '1280.50元', 128051), slot('m2', '1280.50元', True)]:
            wrong = deepcopy(self.second); wrong['slots']['amount_minor'] = changed
            with self.assertRaises(ValueError):
                validate([wrong], self.c2)
        both = build('both', 'both', [{'message_id': 'both', 'text': '3500元还是1280元'}], [], read_only=False)
        with self.assertRaisesRegex(ValueError, 'UNBOUND_SLOT_AMOUNT'):
            validate([item({'amount_minor': slot('both', '3500元还是1280元', 350000)},
                           ['source_account', 'target_account'])], both)

    def test_conflict_is_not_missing_or_a_usable_amount(self):
        conflict = item(parent='m2:transfer', conflicts=['amount_minor'])
        decision = validate([conflict], self.c3)
        self.assertNotIn('amount_minor', decision['items'][0]['slots'])
        self.assertEqual((), assumptions(decision, self.c3).selected)
        document = response(self.c3, [conflict], {'format': 'request_clarification_v1', 'item_ids': ['transfer']})
        result = validate_response(document, self.c3)
        self.assertTrue(result['ok'], result)
        self.assertIn('冲突', result['text'])
        self.assertNotIn('请补充', result['text'])

    def test_clarification_cannot_hide_write_or_drop_an_item(self):
        answer = {'format': 'request_clarification_v1', 'item_ids': ['transfer']}
        raw = response(self.c1, [self.first], answer)
        self.assertTrue(validate_response(raw, self.c1)['ok'])
        raw['trace'] = [{'kind': 'request', 'name': 'prepare_transfer', 'arguments': {}}]
        self.assertFalse(validate_response(raw, self.c1)['ok'])
        second = item(missing=['merchant_text'], identifier='subscription', intent='mandate_cancel')
        raw = response(self.c1, [self.first, second], answer)
        self.assertFalse(validate_response(raw, self.c1)['ok'])

    def test_request_id_and_duplicate_json_keys_rejected(self):
        raw = response(self.c1, [self.first], {'format': 'request_clarification_v1', 'item_ids': ['transfer']})
        other = deepcopy(self.c1); other['request_id'] = 'other'
        self.assertFalse(validate_response(raw, other)['ok'])
        raw['text'] = raw['text'].replace('"request_id": "m1"', '"request_id": "m1", "request_id": "m1"')
        self.assertEqual('DUPLICATE_JSON_KEY', validate_response(raw, self.c1)['answer_validation']['reason'])
