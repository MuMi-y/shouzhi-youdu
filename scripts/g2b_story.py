"""Stepwise G2B acceptance. Default plan is read-only; no model calls without --execute-live."""
import argparse
import base64
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import secrets
import shutil
import sqlite3
import sys
import threading

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.g2b_control import (Control, ControlError, ControlledRunner, BusinessClock,
                                 exclusive_lease, save_json, wall_ms)
from scripts.competition_acceptance_live import Scenario, facts, source_hashes, DEFAULT_SPEC
from scripts.g1_goal_demo import seed_new_fixture
from src.bank_insights.fixture import create_demo
from src.bank_v1 import Service
from src.bank_v1.security import enroll, SecurityPolicy, TotpPasswordVerifier
from src.bank_v1_web.worker import DshRunner, Worker
from src.competition_web.app import create_app
from src.competition_web.pump import CompetitionPump


G01 = '我想在11月22日买一台4500元的学习电脑，最低3000元也可以，最晚11月30日，至少保留2000元，先保证还款。请帮我整理目标并比较安排，先不要替我保存或办理。'


def original_spec():
    data = json.loads(DEFAULT_SPEC.read_text(encoding='utf-8'))
    return data, next(case for case in data['cases'] if case['id'] == 'S03')


def versions():
    result = source_hashes()
    for name in ('scripts/g2b_control.py', 'scripts/g2b_story.py', 'scripts/g2b_sync.py',
                 'scripts/g2b_view.py', 'scripts/sandbox_layout.cjs', 'scripts/check_sandbox_runtime.cjs',
                 'management/evidence/COMPETITION_V1/acceptance/cases.json',
                 'management/evidence/COMPETITION_V1/acceptance/ai_focus_v3_cases.json'):
        result[name] = hashlib.sha256((ROOT / name).read_bytes()).hexdigest()
    return result


def branch_config(branch):
    if branch == 'G':
        return '2026-11-02T10:00:00+08:00', {'fixture': 'G1', 'goals': True}
    if branch == 'S03':
        spec, case = original_spec()
        return spec['business_clock'], {'fixture': 'standard', 'goals': False, 'case': case}
    if branch == 'H01':
        return '2026-12-28T10:00:00+08:00', {'fixture': 'H01', 'goals': True,
            'holdout': True, 'profile': {
                'title': '学习设备', 'user_reason': '学习使用', 'priority_reason': '先保留固定开支和备用金',
                'preferred_amount_minor': 386743, 'min_amount_minor': 281119,
                'preferred_date': '2027-01-02', 'latest_date': '2027-01-08', 'reserve_floor_minor': 120017}}
    if branch == 'H02':
        clock, config = branch_config('H01')
        return clock, {**config, 'fixture': 'H01_COPY_NEW_CHECK', 'source_branch': 'H01'}
    raise ControlError('UNKNOWN_BRANCH')


def seed_holdout(path, clock):
    """Only materialize the fixed, previously approved input; no expected output computation."""
    if path.exists(): raise ControlError('HOLDOUT_FIXTURE_ALREADY_EXISTS')
    bank = Service(path, clock=clock)
    now = clock().isoformat()
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('DELETE FROM transactions')
        db.execute('DELETE FROM arrangements')
        for account, amount in [('A', 731247), ('B', 128816)]:
            db.execute('UPDATE accounts SET balance_minor=? WHERE account_id=?', (amount, account))
            db.execute('INSERT INTO transactions VALUES(?,?,?,?,?,?,?,?,?,?)',
                       ('h01-opening-' + account, account, 'OPENING', amount, 'H01虚构期初',
                        '2026-12-28', now, None, None, 'TEST_FIXTURE'))
        for aid, kind, amount, date in [('expense1', 'expense', 176329, '2026-12-30'),
                                       ('expense2', 'expense', 240013, '2027-01-05'),
                                       ('income', 'income', 219971, '2027-01-03')]:
            db.execute('INSERT INTO arrangements VALUES(?,?,?,?,?,?,?,?,?)',
                       (aid, 1, aid, kind, amount, date, now, 'TEST_FIXTURE', 'active'))


def initialize_branch(control, branch, *, runtime_root=None):
    clock_value, config = branch_config(branch)
    if control.status()['run']['mode'] == 'offline' and branch in {'H01', 'H02'}:
        raise ControlError('HOLDOUT_NOT_RUN_WITH_OFFLINE_SUBSTITUTES')
    if runtime_root is None and control.status()['run']['mode'] == 'live':
        runtime_root = ROOT / 'runtime' / 'g2b' / control.status()['run']['id']
    info = control.add_branch(branch, clock_value, config, runtime_root=runtime_root)
    step = control.begin_step(branch, 'initialize', {'actor': 'TEST_FIXTURE', 'config': config})
    directory = Path(info['runtime'])
    try:
        directory.mkdir(parents=True, exist_ok=False)
        clock = BusinessClock(clock_value)
        if branch == 'G': seed_new_fixture(directory / 'bank.sqlite3', clock())
        elif branch == 'S03': create_demo(directory / 'bank.sqlite3', clock=clock)
        elif branch == 'H01': seed_holdout(directory / 'bank.sqlite3', clock)
        elif branch == 'H02':
            # Copy bank facts from H01, but create fresh goal/check/job stores through original APIs.
            with StepRuntime(control, 'H01') as original:
                if any(j['status'] in {'RUNNING', 'QUEUED'} for j in original.jobs.recent(limit=500)):
                    raise ControlError('H01_SOURCE_NOT_QUIESCENT')
                source_goal = original.facade.goals.state()['goal']
                if not source_goal or source_goal['profile'] != config['profile']:
                    raise ControlError('H01_SOURCE_PROFILE_CHANGED')
                source_path = original.directory / 'bank.sqlite3'
            with closing(sqlite3.connect(source_path.as_uri() + '?mode=ro', uri=True)) as source_db:
                with closing(sqlite3.connect(directory / 'bank.sqlite3')) as copied_db:
                    source_db.backup(copied_db)
        password = secrets.token_urlsafe(24)
        otp = base64.b32encode(secrets.token_bytes(20)).decode('ascii')
        enroll(directory / 'test-fixture-auth.sqlite3', password, otp, provenance='TEST_FIXTURE')
        # These local factors are never included in evidence or sent to the model.
        save_json(directory / '.fixture-factors.json', {'password': password, 'otp': otp})
        save_json(directory / 'story-state.json', {'session': '', 'turns': [], 'actions': [],
                   'seen_operations': [], 'processed_actions': 0})
        with StepRuntime(control, branch) as runtime:
            runtime.jobs.set_setting('schedule', {'enabled': False, 'cadence': 'daily'})
            if branch in {'H01', 'H02'}:
                runtime.facade.goals.action('save', {'expected_revision': 0, 'profile': config['profile']})
            if branch == 'S03': runtime.facade.insights.check()
            snapshot = runtime.capture()
        save_json(directory / 'initial-snapshot.json', snapshot)
        control.finish_step(step, {'step_id': step, 'actor': 'TEST_FIXTURE', 'after': snapshot,
                                   'real_model_calls': 0, 'config': config})
    except Exception as error:
        control.finish_step(step, {'error': type(error).__name__, 'message': str(error)}, error='INITIALIZATION_FAILED')
        raise
    return step


class StepRuntime(Scenario):
    """Reuse original S03 snapshot/drain/trusted_actions, without its fresh-only constructor/ticker."""
    def __init__(self, control, branch):
        self.control = control
        info = control.branch(branch)
        self.branch_id, self.directory = branch, Path(info['runtime'])
        if not (self.directory / 'bank.sqlite3').is_file(): raise ControlError('BRANCH_BANK_MISSING')
        config = json.loads(info['config_json'])
        self.clock = BusinessClock(info['business_clock'])
        self.case = config.get('case')
        self.auth_file = self.directory / 'test-fixture-auth.sqlite3'
        factors = json.loads((self.directory / '.fixture-factors.json').read_text(encoding='utf-8'))
        self.password, self.otp_secret = factors['password'], factors['otp']
        self.bank = Service(self.directory / 'bank.sqlite3', clock=self.clock,
                            security_policy=SecurityPolicy(mode='independent', verifier=TotpPasswordVerifier(self.auth_file)))
        self.app = create_app(self.directory, clock=self.clock, bank=self.bank,
                              goals_enabled=config['goals'], goal_followup=config['goals'], goal_ai=config['goals'])
        self.facade = self.app.extensions['competition']
        self.jobs = self.app.extensions['bank_v1']['jobs']
        self.pump = CompetitionPump(self.facade, self.jobs, clock=self.clock)
        self.saved = json.loads((self.directory / 'story-state.json').read_text(encoding='utf-8'))
        self.action_log = self.saved['actions']
        self.seen_operations = set(self.saved['seen_operations'])
        self.lifecycle_errors, self.http_tools = [], []
        self.server = self.server_thread = None
        self.trace_path = None
        self.observer = None
        self.trace_lock = threading.Lock()
        self.closing = threading.Event()
        from flask import request
        @self.app.before_request
        def reject_during_close():
            if self.closing.is_set():
                return {'error': 'ACCEPTANCE_SERVER_STOPPING'}, 503, {'Connection': 'close'}
        @self.app.after_request
        def audit(response):
            if request.path.startswith('/api/tools/'):
                entry = {'at': self.clock().isoformat(), 'real_at_ms': wall_ms(),
                         'tool': request.path.rsplit('/', 1)[-1], 'args': request.get_json(silent=True),
                         'status': response.status_code, 'result': response.get_json(silent=True)}
                with self.trace_lock:
                    self.http_tools.append(entry)
                    if self.trace_path:
                        import os
                        with self.trace_path.open('a', encoding='utf-8') as handle:
                            handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
                            handle.flush()
                            os.fsync(handle.fileno())
                if self.observer is not None:
                    self.observer.observe(self.branch_id, entry['tool'], entry['args'], entry['status'], entry['result'])
            return response

    def __enter__(self): return self

    def __exit__(self, *exc): self.close()

    def set_observer(self, observer):
        if self.server is not None or self.observer is not None:
            raise ControlError('OBSERVER_MUST_BE_EXPLICIT_BEFORE_HTTP')
        self.observer = observer

    def start_http(self, step):
        from waitress import create_server
        self.trace_path = self.control.directory / 'evidence' / (step + '-http.jsonl')
        self.trace_path.parent.mkdir(parents=True, exist_ok=True)
        self.server = create_server(self.app, map={}, host='127.0.0.1', port=0, threads=4)
        self.base = f'http://127.0.0.1:{self.server.effective_port}'
        self.server_thread = threading.Thread(target=self.server.run, daemon=True, name='g2b-http')
        self.server_thread.start()

    def model_runner(self, step, *, execute_live):
        if not execute_live or self.control.status()['run']['mode'] != 'live':
            raise ControlError('EXPLICIT_LIVE_RUN_REQUIRED')
        self.start_http(step)
        return ControlledRunner(self.control, step, self.http_runner())

    def http_runner(self):
        """Construct the same scoped runner as the product; does not invoke it."""
        if self.server is None:
            raise ControlError('HTTP_NOT_STARTED')
        run = self.control.status()['run']
        return DshRunner(ROOT, self.directory, self.base, self.app.extensions['bank_v1']['tool_token'],
                         sandbox=True, batch_budget=run['budget_path'], timeout=210,
                         request_gateway=self.app.extensions['bank_v1']['service'].request_gateway)

    def capture(self):
        value = self.snapshot(drain_tasks=False)
        value.update(business_clock=self.clock().isoformat(), jobs=self.jobs.recent(limit=500))
        if self.facade.goals:
            value['goal'] = self.facade.goals.state()
            value['followup'] = self.facade.goal_followup.state()
            value['drafts'] = self.facade.goal_tools.drafts()
        return value

    def persist(self):
        self.saved.update(actions=self.action_log, seen_operations=sorted(self.seen_operations))
        save_json(self.directory / 'story-state.json', self.saved)

    def close(self):
        if self.server is not None:
            self.closing.set()
            server = self.server
            server.task_dispatcher.shutdown(timeout=5)
            closed, errors = threading.Event(), []
            def close_owned_channels():
                try:
                    server.asyncore.close_all(map=server._map, ignore_all=False)
                except Exception as error:
                    errors.append(type(error).__name__)
                finally:
                    closed.set()
            # Waitress close() alone leaves keep-alive channels in its private map.
            # Close that map on its own event thread, never the global socket map.
            server.trigger.pull_trigger(close_owned_channels)
            closed.wait(timeout=3)
            self.server_thread.join(timeout=3)
            if (not closed.is_set() or errors or self.server_thread.is_alive()
                    or server.task_dispatcher.threads or server._map):
                raise ControlError('HTTP_SHUTDOWN_NOT_CONFIRMED')
            self.server = None


def trusted(runtime, operation, payload):
    """Explicit test actor entry, separate from the AI tool router."""
    if operation.startswith('goal-'):
        action = operation.removeprefix('goal-')
        if action not in {'save', 'compare', 'select', 'prepare', 'reconcile'}:
            raise ControlError('TRUSTED_OPERATION_NOT_ALLOWED')
        result = runtime.facade.goals.action(action, payload)
    elif operation.startswith('bank-'):
        action = operation.removeprefix('bank-')
        if action not in {'inject_transaction', 'prepare_match', 'confirm', 'reject'}:
            raise ControlError('TRUSTED_OPERATION_NOT_ALLOWED')
        result = runtime.bank.user_action(action, payload)
    elif operation == 'schedule':
        if set(payload) != {'enabled', 'cadence'} or type(payload['enabled']) is not bool or payload['cadence'] not in {'daily', 'weekly'}:
            raise ControlError('INVALID_SCHEDULE')
        runtime.jobs.set_setting('schedule', payload)
        result = payload
    elif operation == 'adopt-draft':
        # Browser adoption fills a form only. Return the same draft for explicit subsequent goal-save.
        draft = next(d for d in runtime.facade.goal_tools.drafts() if d['draft_id'] == payload['draft_id'])
        result = {'expected_revision': draft['expected_revision'], 'profile': draft['profile'],
                  'saved': False, 'next': '核对后另行 goal-save；采用本身不保存'}
    else:
        raise ControlError('TRUSTED_OPERATION_NOT_ALLOWED')
    runtime.action_log.append({'actor': 'TEST_FIXTURE', 'action': operation, 'input': payload, 'result': result})
    runtime.persist()
    return result


def s03_turn(runtime, runner):
    previous = runtime.saved['turns']
    is_followup = bool(previous)
    if len(previous) >= 4: raise ControlError('S03_FOUR_ROUND_LIMIT')
    actual = [a for a in runtime.action_log if a['action'] in {'confirm', 'reject'} and 'error_code' not in a]
    if previous:
        if len(actual) <= runtime.saved['processed_actions']:
            raise ControlError('S03_FOLLOWUP_REQUIRES_NEW_TRUSTED_ACTION')
        prompt = runtime.case['input']['followup_after_action']
        runtime.saved['processed_actions'] = len(actual)
    else:
        prompt = runtime.case['input']['prompts'][0]
    if callable(getattr(type(runner), 'run_request', None)):
        from src.bank_requests.execution import run_chat
        result = run_chat(runner, runtime.jobs, prompt, runtime.saved['session'], readonly=False)
    else:
        if runtime.control.status()['run']['mode'] == 'live':
            raise ControlError('REQUEST_CONTEXT_RUNNER_REQUIRED')
        result = runner(prompt, runtime.saved['session'], readonly=False)
    runtime.saved['session'] = result.get('session_id') or ''
    runtime.saved['turns'].append({'question': prompt, 'result': result, 'after_test_actor': is_followup})
    runtime.persist()
    return result


def database_rows(directory):
    result = {}
    for path in sorted(Path(directory).glob('*.sqlite3')):
        with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
            tables = [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
            result[path.name] = {}
            for table in tables:
                rows = db.execute('SELECT * FROM "' + table.replace('"', '""') + '"').fetchall()
                result[path.name][table] = hashlib.sha256(repr(sorted(rows, key=repr)).encode()).hexdigest()
    return result


def execute_step(control, branch, kind, payload, *, execute_live=False, runner_factory=None):
    """One explicit step. runner_factory is only for focused offline microfixtures, never CLI."""
    if control.status()['run']['mode'] == 'live' and not execute_live:
        raise ControlError('EXPLICIT_LIVE_RUN_REQUIRED')
    if kind in {'worker', 'g01', 's03-turn', 'h02-old'} and not execute_live and runner_factory is None:
        raise ControlError('OFFLINE_MODEL_DISABLED_USE_PUMP_OR_INSPECT')
    if versions() != json.loads(control.status()['run']['source_hashes_json']):
        control.stop('SOURCE_VERSION_CHANGED')
        raise ControlError('SOURCE_VERSION_CHANGED')
    step = control.begin_step(branch, kind, payload)
    evidence = {'step_id': step, 'run_id': control.status()['run']['id'], 'branch': branch,
                'kind': kind, 'input': payload, 'budget_before': control.status()['run'],
                'quality_review': 'PENDING_REVIEW', 'actor': 'TEST_FIXTURE' if kind in {'trusted', 's03-confirm', 'clock', 'restore'} else 'EXECUTOR'}
    error_code = None
    runtime = None
    try:
        if kind == 'clock':
            control.set_clock(step, payload['value'])
        with StepRuntime(control, branch) as runtime:
            evidence['before'] = runtime.capture()
            calls_before = control.status()['rounds_used']
            if kind == 'h02-old':
                if branch != 'H02' and not (runner_factory is not None and control.status()['run']['mode'] == 'offline'):
                    raise ControlError('H02_BRANCH_ONLY')
                from scripts.g2b_sync import run_stale_step
                make_runner = lambda: runner_factory(runtime, step) if runner_factory else runtime.model_runner(step, execute_live=execute_live)
                evidence['h02'] = run_stale_step(runtime, control, step, payload, make_runner)
                if not evidence['h02']['expected_stale']:
                    error_code = evidence['h02']['reason']
            elif kind in {'worker', 'g01', 's03-turn'}:
                runner = runner_factory(runtime, step) if runner_factory else runtime.model_runner(step, execute_live=execute_live)
                if kind == 's03-turn':
                    if branch != 'S03': raise ControlError('S03_BRANCH_ONLY')
                    evidence['result'] = s03_turn(runtime, runner)
                else:
                    if branch == 'S03': raise ControlError('S03_USES_ORIGINAL_DIRECT_RUNNER')
                    if kind == 'g01':
                        if branch != 'G' or runtime.jobs.setting('g01_added'):
                            raise ControlError('G01_ALREADY_ENQUEUED_OR_WRONG_BRANCH')
                        job = runtime.jobs.add(G01, session_id='')
                        runtime.jobs.set_setting('g01_added', job['id'])
                    worker = Worker(runtime.facade, runtime.jobs, runner, clock=runtime.clock)
                    evidence['worker_advanced'] = worker.step()
            elif kind == 'pump': runtime.pump.step()
            elif kind == 'trusted': evidence['result'] = trusted(runtime, payload['operation'], payload['body'])
            elif kind == 's03-confirm':
                if branch != 'S03': raise ControlError('S03_BRANCH_ONLY')
                action_count = len(runtime.action_log)
                evidence['acted'] = runtime.trusted_actions(len(runtime.saved['turns']))
                runtime.persist()
                if runtime.lifecycle_errors or any('error_code' in a for a in runtime.action_log[action_count:]):
                    raise ControlError('S03_TRUSTED_LIFECYCLE_ERROR')
            elif kind not in {'inspect', 'clock', 'restore', 's03-assess'}:
                raise ControlError('UNKNOWN_STEP')
            evidence.update(after=runtime.capture(), http_tools=runtime.http_tools, trusted_actions=runtime.action_log,
                            lifecycle_errors=runtime.lifecycle_errors, rounds_used=control.status()['rounds_used'] - calls_before)
            if evidence['rounds_used']:
                old = {j['id']: j for j in evidence['before']['jobs']}
                changed = [j for j in evidence['after']['jobs'] if j != old.get(j['id'])]
                evidence['changed_jobs'] = changed
                expected_negative = kind == 'h02-old' and evidence.get('h02', {}).get('expected_stale') is True
                if any(j['status'] in {'FAILED', 'INTERRUPTED'} for j in changed) and not expected_negative:
                    error_code = 'WORKER_RESULT_REJECTED'
            if kind == 's03-assess':
                if branch != 'S03': raise ControlError('S03_BRANCH_ONLY')
                tools = []
                for old_step in control.status()['steps']:
                    if old_step['branch_id'] == 'S03' and Path(old_step['evidence_path']).is_file():
                        tools.extend(json.loads(Path(old_step['evidence_path']).read_text(encoding='utf-8')).get('http_tools', []))
                before = json.loads((runtime.directory / 'initial-snapshot.json').read_text(encoding='utf-8'))
                evidence['original_evaluator'] = facts(runtime.case, before, evidence['after'], tools,
                                                       runtime.action_log, runtime.saved['turns'])
        # All HTTP threads/writers are now stopped; copy the complete branch, not selected databases.
        if kind == 'restore':
            info = control.branch(branch)
            origin = Path(info['runtime'])
            # Runtime/session trees need the same short-path budget as live runs.
            # Archive only the evidence and mapping; never nest a full DSH home
            # below the already-deep evidence directory on Windows.
            target = origin.parent / ('restore-' + step)
            if any(j['status'] == 'RUNNING' for j in evidence['after']['jobs']):
                raise ControlError('RESTORE_REQUIRES_NO_RUNNING_JOB')
            before_rows = database_rows(origin)
            shutil.copytree(origin, target)
            restored_rows = database_rows(target)
            if before_rows != restored_rows: raise ControlError('RESTORE_ROWS_DIFFER')
            # Open existing production services on the restored path; no Worker.start/recovery/replay.
            info_clock = BusinessClock(info['business_clock'])
            cfg = json.loads(info['config_json'])
            reopened = create_app(target, clock=info_clock, goals_enabled=cfg['goals'],
                                  goal_followup=cfg['goals'], goal_ai=cfg['goals'],
                                  security_policy=SecurityPolicy(mode='independent', verifier=TotpPasswordVerifier(target / 'test-fixture-auth.sqlite3')))
            reopened.extensions['competition'].state()
            after_rows = database_rows(target)
            if before_rows != after_rows: raise ControlError('RESTORE_OPEN_CHANGED_ROWS')
            evidence['restore'] = {'target': str(target), 'before': before_rows, 'copied': restored_rows,
                                   'reopened': after_rows, 'model_calls': 0, 'bank_actions_replayed': 0,
                                   'controller_rebound': False, 'note': '恢复副本只核验，不新建控制器或重置轮数'}
    except Exception as error:
        error_code = getattr(error, 'code', None) or type(error).__name__ + ':' + str(error)
        evidence['error'] = error_code
        if runtime is not None:
            evidence.update(http_tools=runtime.http_tools, trusted_actions=runtime.action_log,
                            lifecycle_errors=runtime.lifecycle_errors)
            try:
                evidence['after'] = runtime.capture()
            except Exception as snapshot_error:
                evidence['snapshot_error'] = type(snapshot_error).__name__
    evidence['control_after'] = control.status()
    control.finish_step(step, evidence, error=error_code)
    return {'step_id': step, 'error': error_code, 'status': control.status()['run']['state'],
            'evidence': str(control.directory / 'evidence' / (step + '.json'))}


def plan():
    clock_value, case = original_spec()
    return {'mode': 'PLAN_ONLY', 'real_model_calls': 0, 'round_limit': 16,
            'shared_budget': {'provider': 120, 'dsh': 32, 'minutes': 70},
            'G': ['init-branch G', 'g01', 'trusted adopt-draft / goal-save', 'pump', 'worker',
                  'trusted goal-compare/select/prepare + bank-confirm + goal-reconcile', 'pump', 'worker',
                  'trusted bank-inject_transaction 600元', 'pump', 'worker',
                  'trusted bank-inject_transaction 0.17元', 'pump（先检查是否静默）',
                  'clock 11月16日 + trusted bank-inject_transaction/prepare_match/confirm', 'pump', 'worker',
                  'trusted schedule + clock + pump + worker（仅有新作业时）', 'restore'],
            'S03': {'input': case['input'], 'sequence': ['s03-turn', 'pump', 's03-confirm',
                    'review', 's03-turn（有新的真实可信动作才可，最多3次跟进）', 's03-assess'],
                    'original_case_unmodified': True},
            'H01': {'status': 'INPUT_DEFINED_NOT_EXECUTED', 'sequence': ['init-branch H01', 'pump', 'worker']},
            'H02': {'status': 'SYNC_IMPLEMENTED_REAL_NOT_EXECUTED',
                    'sequence': ['init-branch H02（复制已完成H01事实，新检查库）', 'pump',
                                 'h02-old（默认最低额269723分；服务端取证屏障）', 'review', 'pump', 'worker'],
                    'observation': '服务端已生成旧事实响应，不声称模型已读；只放行结构化预期STALE'},
            'after_every_step': 'read evidence; review STEP --accept --note ... or --reject; no automatic next step',
            'live_batch_status': 'NOT_EXECUTED'}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', nargs='?', default='plan', choices=['plan', 'init', 'status', 'init-branch',
                        'review', 'stop', 'recover', 'pump', 'worker', 'g01', 's03-turn', 's03-confirm',
                        'trusted', 'clock', 'inspect', 'restore', 's03-assess', 'h02-old'])
    parser.add_argument('--control', type=Path)
    parser.add_argument('--branch', choices=['G', 'S03', 'H01', 'H02'])
    parser.add_argument('--mode', choices=['offline', 'live'], default='offline')
    parser.add_argument('--batch-budget', type=Path)
    parser.add_argument('--runtime-root', type=Path, help='Fresh branch parent, separate from archived evidence')
    parser.add_argument('--execute-live', action='store_true')
    parser.add_argument('--step')
    parser.add_argument('--note', default='')
    verdict = parser.add_mutually_exclusive_group()
    verdict.add_argument('--accept', action='store_true')
    verdict.add_argument('--reject', action='store_true')
    parser.add_argument('--payload', type=Path, help='UTF-8 JSON: trusted={operation,body}; clock={value}')
    args = parser.parse_args()
    if args.command == 'plan':
        print(json.dumps(plan(), ensure_ascii=False, indent=2)); return
    if args.control is None: parser.error('--control is required')
    if args.command == 'init':
        if args.mode == 'live' and not args.execute_live: parser.error('live init requires --execute-live')
        control = Control.create(args.control, mode=args.mode, budget_path=args.batch_budget, source_hashes=versions())
        print(json.dumps(control.status(), ensure_ascii=False, indent=2)); return
    control = Control(args.control)
    if args.command == 'status':
        print(json.dumps(control.status(), ensure_ascii=False, indent=2)); return
    with exclusive_lease(args.control):
        if args.command == 'review':
            if not args.step or not (args.accept or args.reject): parser.error('review needs --step and --accept/--reject')
            result = control.review(args.step, accept=args.accept, note=args.note)
        elif args.command == 'stop':
            control.stop(args.note or 'OPERATOR_STOP'); result = control.status()
        elif args.command == 'recover':
            result = {'uncertain_steps': control.recover_uncertain(), 'status': control.status()}
        else:
            if not args.branch: parser.error('--branch is required')
            if control.status()['run']['mode'] == 'live' and not args.execute_live:
                parser.error('live mutation requires --execute-live')
            if args.command == 'init-branch':
                result = {'step_id': initialize_branch(control, args.branch, runtime_root=args.runtime_root), 'status': control.status()}
            else:
                payload = json.loads(args.payload.read_text(encoding='utf-8-sig')) if args.payload else {}
                result = execute_step(control, args.branch, args.command, payload, execute_live=args.execute_live)
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try: main()
    except (ControlError, ValueError, FileExistsError) as error:
        print(json.dumps({'ok': False, 'error': str(error), 'type': type(error).__name__}, ensure_ascii=False))
        raise SystemExit(2)
