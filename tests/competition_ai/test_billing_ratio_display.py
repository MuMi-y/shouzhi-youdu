"""Portable display-contract checks with independent rational rounding oracles."""
from copy import deepcopy
from decimal import localcontext, ROUND_UP
from fractions import Fraction
from pathlib import Path
import json
import random
import unittest

from src.bank_insights.ratio_display import ratio_display
from src.bank_v1_web.bank_answer import validate_bank_answer
from tests.competition_insights.test_billing_evidence import report, row, gate


def sample():
    return report([row('expense', -705500), row('income', 800000, 'income'),
                   row('old-expense', -321000, day='2026-08-15'),
                   row('old-income', 700000, 'income', day='2026-08-15')])


def envelope(data, *, ok=True, scope='bank'):
    return {'kind': 'response', 'tool': 'billing_report', 'ok': ok,
            'response': {'scope': scope, 'data': {'report': data}}}


class RatioDisplayTests(unittest.TestCase):
    def test_raw_ratio_not_double_rounded_and_context_independent(self):
        for numerator, expected in [(885001, '89'), (885000, '88'), (895000, '90'), (-885001, '-89')]:
            with self.subTest(numerator=numerator), localcontext() as context:
                context.prec, context.rounding = 2, ROUND_UP
                value = ratio_display(numerator, 1000000, label='本期净流入占收入')
                self.assertEqual(expected, value['rounded_value'])
                self.assertEqual(2, context.prec)

    def test_independent_fraction_oracle_including_sign_and_ties(self):
        rng = random.Random(20261019)
        for _ in range(150):
            numerator, denominator = rng.randint(-10000000, 10000000), rng.randint(1, 10000000)
            value = ratio_display(numerator, denominator, label='本期净流入占收入')
            self.assertEqual(str(round(Fraction(numerator * 100, denominator))), value['rounded_value'])
            hundredths = round(Fraction(numerator * 10000, denominator))
            magnitude = abs(hundredths)
            expected = ('-' if hundredths < 0 else '') + f'{magnitude // 100}.{magnitude % 100:02d}%'
            self.assertEqual(expected, value['exact_text'])

    def test_undefined_ratio_and_unnatural_cheng_not_generated(self):
        for denominator in [0, -10, True]:
            self.assertIsNone(ratio_display(10, denominator, label='本期净流入占收入'))
        for numerator in [-100, 1, 15000]:
            self.assertEqual(1, len(ratio_display(numerator, 10000, label='本期净流入占收入')['phrases']))

    def test_tool_generated_current_and_previous_phrases_accepted(self):
        data = sample()
        for period in [data, data['previous_period']]:
            for metric in period['metrics']:
                for text in metric['display']['phrases']:
                    result = gate(text, data)
                    self.assertTrue(result['ok'], result.get('answer_validation'))
                    checks = result['answer_validation']['billing_ratio_display_checks']['checks']
                    self.assertEqual('VERIFIED_DISPLAY', checks[0]['status'])
                    self.assertEqual(metric['period'], checks[0]['period'])

    def test_emphasis_and_ascii_parentheses_preserve_evidence(self):
        text = sample()['metrics'][0]['display']['phrases'][1]
        self.assertTrue(gate('**' + text.replace('（', '(').replace('）', ')') + '**', sample())['ok'])

    def test_horizontal_token_whitespace_is_layout_not_new_numbers(self):
        for text in ['本期消费支出占收入约 88%（88.19%）',
                     '本期消费支出占收入 约\t八成八 （ 88.19 % ）',
                     '本期消费支出占收入约\u300088 ％ （88.19％）']:
            with self.subTest(text=text):
                self.assertTrue(gate(text, sample())['ok'])

    def test_split_number_or_newline_is_not_reassembled(self):
        for text in ['本期消费支出占收入约8 8%（88.19%）',
                     '本期消费支出占收入约88%\n（88.19%）',
                     '本期消费支出占收入约八成八；（88.19%）',
                     '本期消费支出占收入约八成八（88 .19%）']:
            with self.subTest(text=text):
                self.assertFalse(gate(text, sample())['ok'])

    def test_real_v27_whitespace_false_rejection_corrected_not_semantic_certification(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/b02_display_spaces_20261019.json').read_text(encoding='utf-8'))
        result = validate_bank_answer(fixture['result'], fixture['question'])
        self.assertTrue(result['ok'])
        self.assertEqual(2, len(result['answer_validation']['billing_ratio_display_checks']['checks']))
        # This proves numeric display only; original income-heading ambiguity
        # remains recorded in the execution review, not relabelled as a live PASS.

    def test_bare_rounded_values_and_wrong_anchor_rejected(self):
        for text in ['本期消费支出占收入88%。', '本期消费支出占收入八成八。',
                     '本期消费支出占收入约八成八。', '本期消费支出占收入约八成八（88.18%）。',
                     '本期消费支出占收入约八成九（88.19%）。']:
            with self.subTest(text=text):
                self.assertFalse(gate(text, sample())['ok'])

    def test_wrong_metric_or_period_is_not_approved_phrase(self):
        phrase = sample()['metrics'][0]['display']['phrases'][1]
        for text in [phrase.replace('本期', '前期'), phrase.replace('消费支出', '净流入'),
                     phrase.replace('消费支出', '储蓄')]:
            self.assertFalse(gate(text, sample())['ok'])

    def test_display_does_not_whitelist_numbers_elsewhere(self):
        text = sample()['metrics'][0]['display']['phrases'][1]
        self.assertFalse(gate(text + '。另一个比例为88%。', sample())['ok'])
        self.assertFalse(gate(text + '。余额是1234.00元。', sample())['ok'])
        self.assertFalse(gate(text + '。转账已成功。', sample())['ok'])

    def test_absent_tampered_or_policy_display_cannot_authorize(self):
        original = sample()
        phrase = original['metrics'][0]['display']['phrases'][1]
        for kind in ['missing', 'tampered', 'policy', 'value', 'total', 'path', 'period']:
            data = deepcopy(original)
            metric = data['metrics'][0]
            if kind == 'missing': metric.pop('display')
            if kind == 'tampered': metric['display']['rounded_value'] = '89'
            if kind == 'policy': data['analysis_policy']['display'] = metric.pop('display')
            if kind == 'value': metric['value_percent'] = '88.18'
            if kind == 'total': data['totals']['expense_minor'] += 10000
            if kind == 'path': metric['numerator_path'] = 'totals.net_change_minor'
            if kind == 'period': metric['period']['start_date'] = '2025-09-01'
            with self.subTest(kind=kind):
                self.assertFalse(gate(phrase, data)['ok'])

    def test_failed_foreign_and_superseded_reports_not_sources(self):
        data = sample()
        text = data['metrics'][0]['display']['phrases'][1]
        newer = report([row('expense', -500000), row('income', 800000, 'income')])
        for trace in [[envelope(data, ok=False)], [envelope(data, scope='history')],
                      [envelope(data), envelope(data, ok=False)], [envelope(data), envelope(newer)]]:
            result = validate_bank_answer({'ok': True, 'text': text, 'trace': trace}, '')
            self.assertFalse(result['ok'])

    def test_unqueried_previous_cannot_authorize_display(self):
        data = sample()
        text = data['previous_period']['metrics'][0]['display']['phrases'][1]
        data['comparison_status'] = 'NOT_REQUESTED'
        self.assertFalse(gate(text, data)['ok'])

    def test_original_b08_failure_is_not_relabelled_as_success(self):
        fixture = json.loads((Path(__file__).parent / 'fixtures/b08_ratio_failure_20261019.json').read_text(encoding='utf-8'))
        result = validate_bank_answer(fixture['result'], fixture['question'])
        self.assertFalse(result['ok'])
        self.assertIn('八成八', str(result['answer_validation']['reasons']))


if __name__ == '__main__':
    unittest.main()
