"""Check evaluation control flow without model, bank, or external requests."""
import importlib.util
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('competition_eval_harness', Path(__file__).resolve().parents[2] / 'scripts' / 'competition_acceptance_live.py')
harness = importlib.util.module_from_spec(spec)
spec.loader.exec_module(harness)


class FakeRunner:
    def __init__(self, results):
        self.results = iter(results)
        self.calls = []

    def __call__(self, prompt, session, readonly=False):
        self.calls.append((prompt, session))
        return next(self.results)


class FakeScenario:
    def __init__(self, results):
        self.runner = FakeRunner(results)
        self.action_log = []
        self.http_tools = []
        self.lifecycle_errors = []
        self.drains = 0
        self.closed = False

    def snapshot(self, *, drain_tasks=True):
        return {'preserved': True}

    def stop_task_progress(self):
        return True

    def drain(self):
        self.drains += 1

    def trusted_actions(self, index):
        self.action_log.append({'action': 'confirm', 'index': index})

    def close(self):
        self.closed = True


class FailedTurnTests(unittest.TestCase):
    def run_fixture(self, results, *, prompts=None, quota_stop=None):
        scenario = FakeScenario(results)
        case = {'id': 'fixture', 'title': 'control flow', 'input': {
            'prompts': prompts or ['first'], 'followup_after_action': 'after action'},
            'trusted_fixture_action': {'mode': 'reject_then_confirm'}, 'expected': {}}
        summary = lambda *args: {'automated_checks': {'all_model_turns_completed': all(t['result'].get('ok') for t in args[-1])}}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'case'
            with patch.object(harness, 'Scenario', return_value=scenario), patch.object(harness, 'source_hashes', return_value={}), patch.object(harness, 'facts', side_effect=summary):
                result = harness.run_case(case, output, 'unused', 210, quota_stop)
            saved = json.loads((output/'turns.json').read_text(encoding='utf-8'))
        self.assertTrue(scenario.closed)
        return scenario, result, saved

    def test_failed_first_turn_prevents_fixture_confirmation_and_followup(self):
        bad = {'ok': False, 'error': 'timeout', 'session_id': 'failed-session'}
        scenario, result, saved = self.run_fixture([bad], prompts=['first', 'must not run'])
        self.assertEqual([('first', '')], scenario.runner.calls)
        self.assertEqual([], scenario.action_log)
        self.assertEqual(0, scenario.drains)
        self.assertEqual(bad, saved[0]['result'])
        self.assertTrue(result['automated_checks']['not_interrupted_by_quota'])
        self.assertFalse(result['quota_failure'])
        self.assertTrue(result['stopped_after_failed_turn'])
        self.assertEqual('FAIL', result['automatic_gate'])

    def test_failed_followup_preserves_previous_confirmation_without_replay(self):
        good = {'ok': True, 'session_id': 'good-session'}
        bad = {'ok': False, 'error': 'rejected', 'session_id': 'failed-session'}
        scenario, result, saved = self.run_fixture([good, bad])
        self.assertEqual([('first', ''), ('after action', 'good-session')], scenario.runner.calls)
        self.assertEqual(1, len(scenario.action_log))
        self.assertEqual(1, scenario.drains)
        self.assertEqual(2, len(saved))
        self.assertTrue(result['stopped_after_failed_turn'])
        self.assertEqual('FAIL', result['automatic_gate'])

    def test_quota_failure_stops_even_without_shared_circuit(self):
        scenario, result, _ = self.run_fixture([{'ok': True, 'session_id': 's'}, {'ok': False, 'error': 'AccountQuotaExceeded'}])
        self.assertEqual(2, len(scenario.runner.calls))
        self.assertEqual(1, len(scenario.action_log))
        self.assertTrue(result['quota_failure'])
        self.assertFalse(result['automated_checks']['not_interrupted_by_quota'])
        self.assertFalse(result['interrupted_by_quota'])
        self.assertTrue(result['stopped_after_failed_turn'])

    def test_quota_failure_opens_shared_circuit(self):
        circuit = threading.Event()
        scenario, result, _ = self.run_fixture([{'ok': False, 'error': 'AccountQuotaExceeded'}], quota_stop=circuit)
        self.assertTrue(circuit.is_set())
        self.assertFalse(result['automated_checks']['not_interrupted_by_quota'])
        self.assertTrue(result['quota_failure'])
        self.assertFalse(result['interrupted_by_quota'])
        self.assertEqual([], scenario.action_log)
        self.assertTrue(result['stopped_after_failed_turn'])

    def test_successful_turns_still_reach_bounded_three_followups(self):
        scenario, result, saved = self.run_fixture([{'ok': True, 'session_id': 's'}] * 4)
        self.assertEqual(4, len(scenario.runner.calls))
        self.assertEqual(4, len(scenario.action_log))
        self.assertEqual(4, len(saved))
        self.assertFalse(result['stopped_after_failed_turn'])
        self.assertTrue(result['automated_checks']['not_interrupted_by_quota'])
        self.assertFalse(result['quota_failure'])
        self.assertEqual('PASS', result['automatic_gate'])


class RealSnapshotScenario(FakeScenario):
    # Exercise the actual production-class snapshot, not a constant fake.
    snapshot = harness.Scenario.snapshot

    def __init__(self, results):
        super().__init__(results)
        self.bank = SimpleNamespace(state=lambda: {'previous_action': 'preserved'})
        self.facade = SimpleNamespace(tasks=SimpleNamespace(list=lambda: []),
                                      insights=SimpleNamespace(state=lambda: {}))


class RealLifecycleTests(unittest.TestCase):
    def run_scenario(self, scenario, *, facts_error=False):
        case = {'id': 'lifecycle', 'title': 'snapshot freeze', 'input': {'prompts': ['first', 'second']},
                'trusted_fixture_action': {'mode': 'none'}, 'expected': {}}
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / 'case'
            facts = (lambda *args: (_ for _ in ()).throw(ValueError('facts failed'))) if facts_error else (
                lambda *args: {'automated_checks': {'all_model_turns_completed': all(t['result'].get('ok') for t in args[-1])}})
            with patch.object(harness, 'Scenario', return_value=scenario), patch.object(harness, 'source_hashes', return_value={}), patch.object(harness, 'facts', side_effect=facts):
                result = harness.run_case(case, output, 'unused', 210)
            saved = {p.name: json.loads(p.read_text(encoding='utf-8')) for p in output.glob('*.json')}
        return result, saved

    def test_real_snapshot_keeps_default_and_readonly_option(self):
        scenario = RealSnapshotScenario([])
        default = scenario.snapshot()
        self.assertEqual(1, scenario.drains)
        readonly = scenario.snapshot(drain_tasks=False)
        self.assertEqual(default, readonly)
        self.assertEqual(1, scenario.drains)

    def test_failed_turn_final_real_snapshot_does_not_drain(self):
        scenario = RealSnapshotScenario([{'ok': False, 'error': 'timeout'}])
        result, saved = self.run_scenario(scenario)
        self.assertEqual(1, scenario.drains)  # before snapshot only
        self.assertEqual(1, len(scenario.runner.calls))
        self.assertEqual([], saved['trusted-actions.json'])
        self.assertEqual('timeout', saved['turns.json'][0]['result']['error'])
        self.assertEqual({'previous_action': 'preserved'}, saved['after.json']['bank'])
        self.assertTrue(result['after_snapshot_tick_quiesced'])
        self.assertFalse(result['after_snapshot_task_drain_enabled'])

    def test_real_background_tick_stops_and_does_not_restart(self):
        scenario = object.__new__(harness.Scenario)
        scenario.stop_event = threading.Event()
        scenario.lifecycle_errors = []
        advanced = threading.Event()
        calls = []
        def advance():
            calls.append('advance')
            advanced.set()
        scenario.facade = SimpleNamespace(advance_tasks=advance)
        scenario.tick_thread = threading.Thread(target=scenario._tick_loop, daemon=True)
        scenario.tick_thread.start()
        try:
            self.assertTrue(advanced.wait(2))
            self.assertTrue(scenario.stop_task_progress(timeout=1))
            self.assertFalse(scenario.tick_thread.is_alive())
            self.assertTrue(scenario.stop_event.is_set())
            count = len(calls)
            scenario._tick_loop()  # stopped loop must immediately return
            self.assertEqual(count, len(calls))
        finally:
            scenario.stop_event.set()
            scenario.tick_thread.join(timeout=2)

    def test_inflight_tick_finishes_but_timeout_not_reported_stable(self):
        scenario = object.__new__(harness.Scenario)
        scenario.stop_event = threading.Event()
        scenario.lifecycle_errors = []
        entered, release = threading.Event(), threading.Event()
        effects = []
        def advance():
            entered.set()
            release.wait(3)
            effects.append('previously in-flight advance completed')
        scenario.facade = SimpleNamespace(advance_tasks=advance)
        scenario.tick_thread = threading.Thread(target=scenario._tick_loop, daemon=True)
        scenario.tick_thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.assertFalse(scenario.stop_task_progress(timeout=.01))
            self.assertTrue(scenario.tick_thread.is_alive())
            self.assertEqual('FIXTURE_TICK_STOP_TIMEOUT', scenario.lifecycle_errors[0]['code'])
            self.assertFalse(scenario.lifecycle_errors[0]['snapshot_stable'])
        finally:
            release.set()
            scenario.tick_thread.join(timeout=2)
        self.assertEqual(['previously in-flight advance completed'], effects)
        self.assertTrue(scenario.stop_task_progress(timeout=.01))

    def test_unstopped_fixture_is_marked_and_evidence_not_drained(self):
        scenario = RealSnapshotScenario([{'ok': False, 'error': 'AccountQuotaExceeded'}])
        def not_stopped():
            scenario.lifecycle_errors.append({'code': 'FIXTURE_TICK_STOP_TIMEOUT', 'snapshot_stable': False})
            return False
        scenario.stop_task_progress = not_stopped
        result, saved = self.run_scenario(scenario)
        self.assertFalse(result['after_snapshot_tick_quiesced'])
        self.assertFalse(result['automated_checks']['failed_fixture_tick_stopped'])
        self.assertTrue(result['quota_failure'])
        self.assertEqual(1, scenario.drains)
        self.assertEqual('FAIL', saved['review.json']['automatic_gate'])

    def test_facts_failure_preserves_original_turn_and_prior_actions(self):
        scenario = RealSnapshotScenario([{'ok': True, 'session_id': 's'}, {'ok': False, 'error': 'first real failure'}])
        scenario.action_log = [{'action': 'confirm', 'status': 'SUCCEEDED', 'operation_id': 'already-done'}]
        result, saved = self.run_scenario(scenario, facts_error=True)
        self.assertEqual('ValueError', result['harness_error']['type'])
        self.assertEqual('first real failure', saved['turns.json'][1]['result']['error'])
        self.assertEqual('already-done', saved['trusted-actions.json'][0]['operation_id'])
        self.assertIn('before.json', saved)
        self.assertIn('after.json', saved)
        self.assertTrue(result['stopped_after_failed_turn'])

    def test_after_snapshot_failure_still_saves_turn_tools_and_actions(self):
        scenario = RealSnapshotScenario([{'ok': False, 'error': 'original model rejection'}])
        original = scenario.snapshot
        calls = []
        def snapshot(*, drain_tasks=True):
            calls.append(drain_tasks)
            if len(calls) > 1:
                raise OSError('snapshot unavailable')
            return original(drain_tasks=drain_tasks)
        scenario.snapshot = snapshot
        scenario.http_tools = [{'tool': 'operation', 'status': 200}]
        result, saved = self.run_scenario(scenario)
        self.assertEqual([True, False, False], calls)
        self.assertIn('before.json', saved)
        self.assertNotIn('after.json', saved)
        self.assertEqual('original model rejection', saved['turns.json'][0]['result']['error'])
        self.assertEqual(scenario.http_tools, saved['tool-calls.json'])
        self.assertEqual('OSError', result['harness_error']['type'])
        self.assertIn({'artifact': 'after.json', 'type': 'OSError'}, result['evidence_errors'])

    def test_cleanup_exception_does_not_hide_original_failure(self):
        scenario = RealSnapshotScenario([{'ok': False, 'error': 'first real failure'}])
        def fail_close():
            raise RuntimeError('cleanup failed')
        scenario.close = fail_close
        result, saved = self.run_scenario(scenario)
        self.assertEqual('first real failure', saved['turns.json'][0]['result']['error'])
        self.assertEqual('RuntimeError', result['cleanup_errors'][0]['type'])
        self.assertEqual('FAIL', saved['review.json']['automatic_gate'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
