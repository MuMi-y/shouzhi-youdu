"""One-shot server-read barrier for H02 acceptance; no model or bank implementation here."""
from dataclasses import dataclass, asdict
import json
from queue import Queue, Empty
import threading
import time
from uuid import uuid4

from scripts.g2b_control import ControlError, save_json, encoded, text_hash, wall_ms
from scripts.acceptance_budget import first_attempt_failed
from src.bank_goals.evidence import validate_answer
from src.bank_v1_web.worker import Worker


@dataclass(frozen=True)
class CaptureTarget:
    branch: str
    check_id: str
    job_id: str
    old_revision: int
    old_basis: str
    old_source_id: str


class ReadBarrier:
    def __init__(self, target, path, *, deadline_ms, timeout_seconds=210, release_seconds=15,
                 now_ms=wall_ms, monotonic_ns=time.monotonic_ns):
        if not 0 < timeout_seconds <= 210 or not 0 < release_seconds <= 15:
            raise ControlError('INVALID_SYNC_TIMEOUT')
        self.target, self.path = target, path
        self.deadline_ms, self.now_ms, self.monotonic_ns = deadline_ms, now_ms, monotonic_ns
        self.end_ns = monotonic_ns() + int(timeout_seconds * 1_000_000_000)
        self.release_seconds = release_seconds
        self.messages, self.released = Queue(), threading.Event()
        self.lock = threading.Lock()
        self.record = {'target': asdict(target), 'state': 'WAITING_CAPTURE', 'events': [],
                       'captured_response': None, 'mutation_receipt': None, 'ignored_requests': 0,
                       'duplicate_matches': 0, 'stop_reason': None}

    def remaining(self):
        return max(0, min((self.end_ns - self.monotonic_ns()) / 1e9,
                          (self.deadline_ms - self.now_ms()) / 1000))

    def _event(self, name):
        self.record['events'].append({'name': name, 'monotonic_ns': self.monotonic_ns(), 'wall_ms': self.now_ms()})

    def _save(self):
        save_json(self.path, self.record)

    def snapshot(self):
        with self.lock:
            return json.loads(encoded(self.record))

    def observe(self, branch, tool, args, status, response):
        target = self.target
        data = response.get('data', {}) if isinstance(response, dict) else {}
        matches = (branch == target.branch and tool == 'goal_context' and status == 200 and
                   args == {'goal_id': 'focus', 'check_id': target.check_id} and
                   isinstance(response, dict) and response.get('scope') == 'goal_plan' and
                   response.get('source_id') == target.old_source_id and
                   data.get('revision') == target.old_revision and data.get('basis_hash') == target.old_basis)
        with self.lock:
            if not matches:
                self.record['ignored_requests'] += 1
                return False
            if self.record['captured_response'] is not None:
                self.record['duplicate_matches'] += 1
                return False
            if self.record['state'] != 'WAITING_CAPTURE' or self.remaining() <= 0:
                return False
            self.record.update(state='CAPTURED', request_id=uuid4().hex, request_args=args,
                               captured_response=json.loads(encoded(response)), response_hash=text_hash(encoded(response)))
            self._event('CAPTURED')
            self._save()  # Old response is durable before the coordinator may mutate.
        self.messages.put('CAPTURED')
        if not self.released.wait(min(self.remaining(), self.release_seconds)):
            self.abort('RELEASE_TIMEOUT')
        # The caller always returns its original Flask response; the barrier never transforms it.
        return True

    def committed(self, receipt, new_context):
        with self.lock:
            # Save even a late commit as evidence, but never call it a successful protocol.
            self.record['mutation_receipt'] = receipt
            self.record['new_context'] = new_context
            if self.record['state'] != 'CAPTURED' or self.remaining() <= 0:
                self._save()
                raise ControlError('SYNC_EXPIRED_BEFORE_COMMIT_ACK')
            self.record['state'] = 'COMMITTED'
            self._event('COMMITTED')
            self._save()

    def release(self):
        with self.lock:
            if self.record['state'] != 'COMMITTED': raise ControlError('SYNC_NOT_COMMITTED')
            self.record['state'] = 'RELEASED'
            self._event('RELEASED')
            self._save()
        self.released.set()

    def abort(self, reason):
        try:
            with self.lock:
                self.record['stop_reason'] = self.record['stop_reason'] or reason
                self.record['state'] = 'FAILED'
                self._event('ABORTED')
                self._save()
        finally:
            self.released.set()

    def completed(self):
        with self.lock:
            self._event('COMPLETED')
            self._save()


def selected_target(runtime):
    pending = [j for j in runtime.jobs.recent(limit=500) if j['status'] in {'QUEUED', 'RUNNING'}]
    followup = runtime.facade.goal_followup.state()
    check = followup['check']
    if (len(pending) != 1 or pending[0]['kind'] != 'goal' or pending[0]['status'] != 'QUEUED'
            or not check or check['state'] != 'PENDING' or followup['stale']
            or pending[0]['event_key'] != 'goal:' + check['check_id']):
        raise ControlError('H02_REQUIRES_ONE_CURRENT_QUEUED_GOAL_CHECK')
    context = runtime.facade.goal_tools.call('goal_context', {'goal_id': 'focus', 'check_id': check['check_id']})
    data = context['data']
    return CaptureTarget(runtime.branch_id, check['check_id'], pending[0]['id'], data['revision'],
                         data['basis_hash'], context['source_id'])


def expected_stale(runtime, control, step_id, barrier):
    """All predicates must hold. Never match an error string or revive STOPPED."""
    record = barrier.snapshot()
    target = barrier.target
    state = control.status()
    rounds = [r for r in state['rounds'] if r['step_id'] == step_id]
    if state['run']['state'] != 'ACTIVE' or len(rounds) != 1 or rounds[0]['result_state'] != 'RETURNED':
        return False, 'CONTROL_OR_ROUND_NOT_SUCCESSFUL'
    raw = json.loads(rounds[0]['result_json'])
    if (raw.get('ok') is not True or raw.get('error') or raw.get('errors') or raw.get('process_cleanup_pending')
            or first_attempt_failed([{'result': raw}])):
        return False, 'RAW_MODEL_FAILURE'
    for item in raw.get('trace', []):
        if isinstance(item, dict) and (item.get('kind') == 'error' or
                item.get('kind') == 'response' and (item.get('ok') is not True or item.get('status') != 200)):
            return False, 'TRACE_CONTAINS_FAILURE'
    checked = validate_answer(raw)
    validation = checked.get('answer_validation', {})
    if (not checked.get('ok') or validation.get('scope') != 'goal_plan' or validation.get('status') != 'ACCEPTED'
            or validation.get('source_id') != target.old_source_id or checked.get('goal_basis_hash') != target.old_basis):
        return False, 'OLD_ANSWER_NOT_VALID_BOUND_EVIDENCE'
    matching = [item for item in raw.get('trace', []) if isinstance(item, dict) and item.get('kind') == 'response'
                and item.get('tool') == 'goal_context' and item.get('args') == record.get('request_args')
                and item.get('response') == record.get('captured_response')]
    if not matching: return False, 'CAPTURE_NOT_IN_RUNNER_TRACE'
    events = record['events']
    names = [e['name'] for e in events]
    times = [e['monotonic_ns'] for e in events]
    if (record['stop_reason'] or names != ['CAPTURED', 'COMMITTED', 'RELEASED', 'COMPLETED']
            or not all(a < b for a, b in zip(times, times[1:]))):
        return False, 'SYNC_ORDER_NOT_PROVEN'
    current = runtime.facade.goal_tools.call('goal_context', {'goal_id': 'focus'})
    new = record.get('new_context', {})
    receipt = record.get('mutation_receipt') or {}
    if (current != new or new.get('data', {}).get('revision') != target.old_revision + 1
            or new['data']['basis_hash'] == target.old_basis or receipt.get('revision') != new['data']['revision']
            or receipt.get('profile') != new['data']['profile']):
        return False, 'TRUSTED_MUTATION_NOT_CURRENT'
    check = runtime.facade.goal_followup.state()['check']
    job = runtime.jobs.get(target.job_id)
    product = job.get('result') or {}
    if (not check or check['check_id'] != target.check_id or check['state'] != 'STALE'
            or check['basis_hash'] != target.old_basis or check['goal_revision'] != target.old_revision
            or job['kind'] != 'goal' or job['event_key'] != 'goal:' + target.check_id or job['status'] != 'FAILED'
            or product.get('ok') is not False or product.get('goal_basis_hash') != target.old_basis
            or product.get('answer_validation', {}).get('status') != 'ACCEPTED'
            or product.get('goal_answer') != checked.get('goal_answer')):
        return False, 'PRODUCT_STRUCTURED_STALE_NOT_PROVEN'
    return True, 'EXPECTED_STALE_REVIEW_REQUIRED'


def run_stale_step(runtime, control, step_id, payload, make_runner):
    target = selected_target(runtime)
    minimum = payload.get('new_min_amount_minor', 269723)
    if type(minimum) is not int or minimum <= 0: raise ControlError('INVALID_H02_MUTATION')
    timeout = payload.get('timeout_seconds', 210)
    barrier = ReadBarrier(target, control.directory / 'evidence' / (step_id + '-h02-sync.json'),
                          deadline_ms=control.status()['run']['deadline_ms'], timeout_seconds=timeout,
                          release_seconds=min(15, timeout))
    runtime.set_observer(barrier)
    runner = make_runner()
    worker = Worker(runtime.facade, runtime.jobs, runner, clock=runtime.clock)
    worker_result = {}
    def work():
        try:
            worker_result['advanced'] = worker.step()
        except Exception as error:
            worker_result['error'] = {'type': type(error).__name__, 'message': str(error)}
        finally:
            barrier.messages.put('WORKER_DONE')
    thread = threading.Thread(target=work, name='h02-one-worker-step', daemon=True)
    thread.start()
    problem = None
    try:
        message = barrier.messages.get(timeout=barrier.remaining())
        if message != 'CAPTURED': raise ControlError('H02_NO_MATCHED_CAPTURE')
        if barrier.snapshot()['stop_reason']: raise ControlError('H02_CAPTURE_ALREADY_ABORTED')
        old_profile = barrier.snapshot()['captured_response']['data']['profile']
        body = {'expected_revision': target.old_revision, 'profile': {**old_profile, 'min_amount_minor': minimum}}
        receipt = runtime.facade.goals.action('save', body)  # Original trusted API; never direct SQL.
        runtime.action_log.append({'actor': 'TEST_FIXTURE', 'action': 'H02-goal-save', 'input': body, 'result': receipt})
        runtime.persist()
        current = runtime.facade.goal_tools.call('goal_context', {'goal_id': 'focus'})
        if receipt['revision'] != target.old_revision + 1 or current['data']['basis_hash'] == target.old_basis:
            raise ControlError('H02_MUTATION_DID_NOT_CHANGE_BASIS')
        barrier.committed(receipt, current)
        barrier.release()
        thread.join(timeout=barrier.remaining())
        if thread.is_alive(): raise ControlError('H02_WORKER_DEADLINE')
    except Empty:
        problem = 'H02_CAPTURE_TIMEOUT'
    except Exception as error:
        problem = getattr(error, 'code', None) or type(error).__name__ + ':' + str(error)
    finally:
        if problem: barrier.abort(problem)
        if not barrier.released.is_set(): barrier.abort('H02_RELEASE_NOT_REACHED')
        if thread.is_alive(): thread.join(timeout=15)  # Cleanup only, not permission for another model call.
        if thread.is_alive():
            problem = 'H02_WORKER_CLEANUP_UNCONFIRMED'
            barrier.abort(problem)
        else:
            barrier.completed()
    accepted, reason = (False, problem) if problem else expected_stale(runtime, control, step_id, barrier)
    if not accepted: control.stop(reason or 'H02_PROTOCOL_FAILED')
    return {'expected_stale': accepted, 'reason': reason, 'protocol': barrier.snapshot(),
            'worker': worker_result, 'worker_thread_alive': thread.is_alive(),
            'quality_verdict': 'EXPECTED_NEGATIVE_REVIEW' if accepted else 'FAILED',
            'mode': 'REAL_MODEL' if control.status()['run']['mode'] == 'live' else 'OFFLINE_PROTOCOL_ONLY'}
