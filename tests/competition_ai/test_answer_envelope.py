"""Transport tolerance must not weaken bank evidence, outcomes or worker limits."""
from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from src.bank_evidence.envelope import normalize, FORMATS
from src.bank_v1_web.bank_answer import validate_bank_answer
from src.bank_v1_web.worker import DshRunner
from src.bank_evidence.service_answer import recheck
from tests.competition_ai import test_dialogue_parts as fixture


class AnswerEnvelopeTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.DialoguePartsTests()
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)

    def raw(self, parts):
        return {'ok': True, 'text': json.dumps({'format':'bank_service_v1','parts':parts}),
                'trace': self.f.f.trace}

    def wrapped(self, result, opening='```json', ending='```'):
        return {**result,'text':opening+'\n'+result['text']+'\n'+ending}

    def test_bare_and_complete_fence_deliver_same_verified_answer(self):
        raw=self.raw([self.f.facts(), self.f.guidance()]); before=deepcopy(raw)
        expected=validate_bank_answer(raw,'请先问清楚')
        bank_before=self.f.f.service.bank.state()
        for opening in ['```json','```JSON','```']:
            wrapped=self.wrapped(raw,opening)
            actual=validate_bank_answer(wrapped,'请先问清楚')
            self.assertTrue(actual['ok'],actual)
            self.assertEqual(expected['text'],actual['text'])
            self.assertEqual(wrapped['text'],actual['model_text'])
            self.assertTrue(recheck(actual,self.f.f.service.call_tool)['ok'])
        self.assertEqual(before,raw)
        self.assertEqual(bank_before,self.f.f.service.bank.state())

    def test_all_known_formats_normalize_without_claiming_business_validity(self):
        for kind in FORMATS:
            raw={'text':'  ```json\r\n'+json.dumps({'format':kind})+'\r\n```  '}
            value=normalize(raw)
            self.assertEqual(kind,json.loads(value['text'])['format'])
            self.assertNotIn('answer_validation',value)

    def test_outside_claim_multiple_blocks_bad_language_are_not_discarded(self):
        raw=self.raw([self.f.guidance()]); wrapped=self.wrapped(raw)['text']
        for text in ['已转账成功\n'+wrapped,wrapped+'\n已转账成功',wrapped+'\n'+wrapped,
                     wrapped.replace('```json','```javascript',1),wrapped[:-3],
                     '```json\n{invalid}\n```']:
            result=validate_bank_answer({**raw,'text':text},'请先问清楚')
            self.assertFalse(result['ok'],text)
            self.assertEqual('answer_envelope',result['answer_validation']['scope'])

    def test_duplicate_keys_nonfinite_and_unknown_format_rejected(self):
        for body in ['{"format":"bank_service_v1","format":"bank_facts_v1"}',
                     '{"format":"bank_service_v1","parts":[{"x":1,"x":2}]}',
                     '{"format":"bank_service_v1","value":NaN}',
                     '{"format":"bank_service_v1","value":Infinity}',
                     '{"format":"bank_service_v1","value":1e999}',
                     '{"format":"unknown"}','[]']:
            with self.assertRaises(ValueError):normalize({'text':'```json\n'+body+'\n```'})

    def test_literal_fence_characters_inside_bare_json_are_not_a_wrapper(self):
        raw={'text':json.dumps({'format':'goal_answer_v1','quoted_data':'```json'})}
        self.assertIs(raw,normalize(raw))

    def test_forged_fact_still_fails_inside_valid_envelope(self):
        part=self.f.facts();part['fact_ids']=['fact:invented']
        result=validate_bank_answer(self.wrapped(self.raw([part,self.f.guidance()])), '查账户')
        self.assertFalse(result['ok'])
        self.assertEqual('bank_service',result['answer_validation']['scope'])

    def test_failed_write_cannot_be_hidden_behind_guidance(self):
        self.f.fail()
        result=validate_bank_answer(self.wrapped(self.raw([self.f.guidance()])), '从A转9000到B')
        self.assertFalse(result['ok'])
        self.assertEqual('OMITTED_BANK_TOOL_FAILURE',result['answer_validation']['reason'])

    def test_fenced_failure_outcome_is_still_evidence_bound(self):
        outcome=self.f.fail()
        result=validate_bank_answer(self.wrapped(self.raw([outcome])), '从A转9000到B')
        self.assertTrue(result['ok'],result)
        self.assertIn('银行拒绝',result['text'])
        outcome['error_code']='INVENTED'
        self.assertFalse(validate_bank_answer(self.wrapped(self.raw([outcome])), '从A转9000到B')['ok'])

    def test_failed_model_is_not_promoted_by_formatting(self):
        raw=self.wrapped(self.raw([self.f.guidance()]));raw['ok']=False
        self.assertFalse(validate_bank_answer(raw,'请先问清楚')['ok'])

    def test_worker_does_not_spend_an_extra_model_call_on_bad_envelope(self):
        raw=self.wrapped(self.raw([self.f.guidance()]));raw['text']+='\n另有未经核验的说明'
        runner=DshRunner('.',self.f.f.temp.name,'http://127.0.0.1:1','dummy',timeout=5)
        with patch.object(runner,'_run_model',return_value=raw) as model:
            result=runner('请先问清楚','',readonly=True)
        self.assertFalse(result['ok']);self.assertEqual(1,model.call_count)
