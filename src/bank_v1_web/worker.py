"""One durable local consumer; DSH remains responsible for the model/tool loop."""

from datetime import timedelta, timezone
import json
import os
from pathlib import Path
import subprocess
import threading
import time
from uuid import uuid4
from src.bank_v1 import DomainError
from .errors import failure_message
from .history_answer import validate_history_answer
from .bank_answer import validate_bank_answer
from .bank_repair import repair_prompt
from .bank_execution_retry import execution_retry_prompt


def event_prompt(event_id):
    return (f'银行事件 {event_id} 已发生。请必须调用 event_context 获取该事件及最新确定性预测，'
            '最终只输出bank_service_v1的parts，其中一个bank_event_v1引用本事件event_id和本轮最新event_context.evidence_id；不自由改写事实。'
            '它已经包含本次所需的交易、安排和确定性预测，除有矛盾外不要重复查同一事实。'
            '请以facts确定性摘要为准，用120至220字解释影响和一个下一步，不重复内部编号或状态码。'
            '必须同时说明已确认安排下的缺口与预计收入不到账的风险。只解释实际存在的缺口，不添加泛化风险断语。'
            '如需说明最低余额，使用scenario_minima同一分支的金额与日期，明确主预测或不计未来收入，不把主预测的最低值混入保守口径。'
            '安排的原定日期与预测计入日期须区分：旧支出因履行未确认而保守计入检查日，不表示今日到期、实际扣款或现实已逾期违约；依据arrangement_timing及facts说明，不改写原定日期。'
            '不能把固定还款改称房租，不猜测未记录的计划，不发起转账或变更。')


class DshRunner:
    def __init__(self, project_dir, runtime_dir, api_base, token, *, timeout=210, history_token=None, sandbox=False,
                 batch_budget=None, display_service=None, request_gateway=None):
        self.project_dir, self.runtime_dir = Path(project_dir), Path(runtime_dir)
        self.api_base, self.token, self.timeout = api_base, token, timeout
        self.history_token = history_token
        self.sandbox = sandbox
        self.display_service = display_service
        self.request_gateway = request_gateway if request_gateway is not None else getattr(display_service, 'request_gateway', None)
        self.batch_budget = str(Path(batch_budget).resolve()) if batch_budget else None
        if self.batch_budget and not sandbox:
            raise ValueError('Shared provider budgets require the host sandbox broker.')

    def __call__(self, prompt, session_id, *, readonly=False, data_scope='bank'):
        result = self.analyze(prompt, session_id, readonly=readonly, data_scope=data_scope)
        if self.display_service is not None and data_scope == 'bank':
            from .display_check import check
            return check(result, self.display_service)
        return result

    def analyze(self, prompt, session_id, *, readonly=False, data_scope='bank'):
        if data_scope == 'bank':
            if not readonly or not self.request_gateway or not self.request_gateway.store:
                return {'ok':False,'error':{'code':'SCOPED_REQUEST_REQUIRED','message':'银行分析须由宿主请求入口启动。'}}
            store = self.request_gateway.store
            from .handoff_contracts import digest
            handle = store.open_host_run('SYSTEM',{'prompt_hash':digest(prompt)},readonly=True)
            result = self.run_typed(prompt,handle)
            store.close(handle,result)
            store.producers_finished(handle,trace_finalized=not result.get('process_cleanup_pending'))
            return result
        started = time.monotonic()
        result = self._run_model(prompt, session_id, readonly=readonly, data_scope=data_scope)
        # Product Worker and standalone acceptance use this exact same boundary.
        if data_scope != 'bank':
            return result
        initial = validate_bank_answer(result, prompt)
        if initial.get('answer_validation', {}).get('scope') in {'goal_plan', 'goal_preview', 'task_progress', 'bank_facts', 'bank_billing', 'bank_spending_comparison', 'bank_service', 'calculation_sources', 'answer_envelope'}:
            return initial  # Typed goal failures need analysis, not the old free-prose repair loop.
        if initial.get('answer_validation', {}).get('status') != 'REJECTED':
            return initial
        remaining = self.timeout - (time.monotonic() - started)
        execution_prompt, retry_reason = execution_retry_prompt(
            result, initial['answer_validation'], prompt, session_id=session_id,
            readonly=readonly, data_scope=data_scope, remaining_seconds=remaining)
        if execution_prompt is not None:
            try:
                retried = self._run_model(execution_prompt, None, readonly=False,
                                          data_scope='bank', timeout=remaining)
            except Exception as error:
                retried = {'ok': False, 'session_id': None,
                    'error': {'code': 'EXECUTION_RETRY_PROCESS_FAILED',
                              'exception_type': type(error).__name__,
                              'message': '执行纠正进程未能完成；原始拒绝已保留，请核对银行面板。'}}
            # Never chain a numeric/read-only repair after this execution attempt.
            checked = validate_bank_answer(retried, prompt)
            return {**checked, 'answer_retry': {'attempted': True,
                'reason': retry_reason, 'mode': 'ORIGINAL_REQUEST_TOOL_EXECUTION',
                'remaining_seconds': round(remaining, 3), 'initial_result': initial,
                'policy': 'ONE_FRESH_EXECUTION_RETRY_ONLY_AFTER_VERIFIED_ZERO_TOOL_ATTEMPTS'}}
        correction, reason = repair_prompt(initial, prompt, remaining)
        if correction is None:
            return {**initial, 'answer_repair': {'attempted': False, 'reason': reason},
                    'answer_retry': {'attempted': False, 'reason': retry_reason,
                                     'mode': 'ORIGINAL_REQUEST_TOOL_EXECUTION'}}
        try:
            corrected = self._run_model(correction, None, readonly=True, data_scope='bank', timeout=remaining)
        except Exception as error:
            # Keep the rejected first result even when starting the corrective
            # process fails. Exception text may contain private environment data.
            corrected = {'ok': False, 'session_id': None,
                'error': {'code': 'REPAIR_PROCESS_FAILED', 'exception_type': type(error).__name__,
                          'message': '补证进程未能正常完成；原始回答和工具记录已保留。'}}
        # The draft in correction is not a user hypothesis; validate against the
        # original request and only the new session's fresh tool responses.
        checked = validate_bank_answer(corrected, prompt)
        return {**checked, 'answer_repair': {'attempted': True, 'reason': reason,
            'remaining_seconds': round(remaining, 3), 'initial_result': initial,
            'policy': 'ONE_FRESH_READONLY_PASS_NO_WRITE_REPLAY'}}

    def run_request(self, prompt, session_id, *, request_context, readonly=False, run_handle=None):
        """Explicit host-owned context; protocol failures never start a paid repair."""
        from src.bank_requests.context import model_prompt
        from src.bank_requests.response import validate
        gateway = self.request_gateway
        if gateway is None or gateway.store is None:
            raise ValueError('SCOPED_REQUEST_REQUIRED')
        store = gateway.store
        handle = run_handle or store.for_job(request_context['request_id'])
        store.validate_handle(handle)
        if request_context.get('run_id') != handle.run_id or request_context.get('source_contract') != handle.source_contract:
            raise ValueError('REQUEST_IDENTITY_MISMATCH')
        session_id = None if handle.read_only else store.jobs.take_session()
        context = {**request_context, 'read_only': bool(readonly or request_context['read_only']),
                   'require_current_action': True}
        current = next(row['text'] for row in context['messages']
                       if row['message_id'] == context['current_message_id'])
        if not context['read_only'] and prompt != current:
            raise ValueError('REQUEST_PROMPT_CONTEXT_MISMATCH')
        tools = getattr(self.display_service, 'goal_tools', None)
        if tools is not None:
            from src.bank_goals import storage
            from src.bank_v1.storage import connection
            from src.bank_goals.impact_contract import requirement
            with connection(tools.goals.db_path) as db:
                goal = storage.goal(db)
            if goal:
                data = tools.call('goal_context', {'goal_id': 'focus'})['data']
                target = {'profile': data['profile'], 'arrangement': (data.get('current_plan') or {}).get('arrangement')}
                context['goal_impact_target'] = target
                context['goal_impact_requirement'] = requirement(current, target)
                if context['goal_impact_requirement']:
                    from src.bank_goals.impact_context import for_request
                    try:
                        context['goal_impact_context'] = for_request(tools.goals, context)
                    except DomainError as error:
                        context['goal_impact_context_error'] = error.code
        supplied, context = model_prompt(context)
        # Retry prose is not a source message; the tool allowlist is read-only.
        gateway = self.request_gateway
        token = gateway.open(context, readonly=context['read_only'], lifetime=min(self.timeout + 10, 600),handle=handle)
        try:
            options = {'readonly': context['read_only'], 'data_scope': 'bank', 'run_handle':handle}
            if token is not None:
                options['tool_token'] = token
            result = self._run_model(supplied, session_id, **options)
        finally:
            if token is not None:
                calls_finished = gateway.close(token)
        if not calls_finished:
            result['process_cleanup_pending'] = True
        result['request_execution_gate'] = {'status': 'SCOPED_RUN_CLOSED' if gateway else 'LEGACY_NOT_CONFIGURED',
                                            'request_id': context['request_id']}
        from src.bank_requests.attempt_projection import collect, disclose
        if result.get('process_cleanup_pending') or result.get('trace_recovery',{}).get('complete') is False:
            store.mark_incomplete(handle.run_id)
        context['_host_attempt_coverage'] = collect(store,handle,result.get('trace',[]))
        result = validate(result, context)
        if self.display_service is not None:
            from .display_check import check
            result = check(result, self.display_service)
        result['host_disclosure'] = disclose(store.snapshot(handle.run_id),self.display_service)
        return result

    def run_typed(self, prompt, handle):
        gateway = self.request_gateway
        if not gateway or not gateway.store or not handle.read_only:
            raise ValueError('SCOPED_REQUEST_REQUIRED')
        gateway.store.validate_handle(handle)
        token = gateway.open(None,readonly=True,lifetime=min(self.timeout+10,600),handle=handle)
        try:
            result = self._run_model(prompt,None,readonly=True,data_scope='bank',tool_token=token,run_handle=handle)
        finally:
            calls_finished = gateway.close(token)
        if not calls_finished:
            result['process_cleanup_pending'] = True
        from src.bank_requests.attempt_projection import collect, disclose
        if result.get('process_cleanup_pending') or result.get('trace_recovery',{}).get('complete') is False:
            gateway.store.mark_incomplete(handle.run_id)
        coverage = collect(gateway.store,handle,result.get('trace',[]))
        result = validate_bank_answer(result,prompt)
        if not coverage['complete']:
            result.update(ok=False,error='工具调用记录尚未完整核对；请查看原记录，不重复办理。')
        result['host_disclosure'] = disclose(gateway.store.snapshot(handle.run_id),self.display_service)
        return result

    def _run_model(self, prompt, session_id, *, readonly=False, data_scope='bank', timeout=None, tool_token=None, run_handle=None):
        if data_scope == 'bank':
            if not self.request_gateway or not self.request_gateway.store or not tool_token:
                raise ValueError('SCOPED_REQUEST_REQUIRED')
            self.request_gateway.store.validate_handle(run_handle)
            if not self.request_gateway.validates(tool_token,run_handle):
                raise ValueError('REQUEST_IDENTITY_MISMATCH')
        budget = self.timeout if timeout is None else timeout
        token = tool_token if tool_token is not None else (self.history_token if data_scope == 'moneydata' else self.token)
        if not token:
            return {'ok': False, 'error': '所选资料范围尚未就绪。'}
        environment = dict(os.environ, BANK_V1_API_BASE=self.api_base, BANK_V1_TOOL_TOKEN=token)
        environment['BANK_V1_SANDBOX'] = '1' if self.sandbox else '0'
        if self.batch_budget:
            environment['BANK_V1_BATCH_BUDGET'] = self.batch_budget
        else:
            environment.pop('BANK_V1_BATCH_BUDGET', None)
        self.runtime_dir.mkdir(parents=True, exist_ok=True)
        from .run_trace import inventory, recover
        prior_runs=inventory(self.runtime_dir)
        cancel_file = self.runtime_dir / ('cancel-' + uuid4().hex)
        environment['BANK_V1_CANCEL_FILE'] = str(cancel_file)
        environment['BANK_V1_READONLY'] = '1' if readonly else '0'
        environment['BANK_V1_DATA_SCOPE'] = data_scope
        # Leave a small cancellation/serialization margin inside the Python
        # deadline. Both attempts share the original wall-clock budget.
        environment['BANK_V1_TIMEOUT_MS'] = str(max(1000, min(300000, int((budget - 3) * 1000))))
        command = ['node', str(self.project_dir / 'scripts/dsh_v1.cjs'), '--runtime', str(self.runtime_dir / 'dsh')]
        process = subprocess.Popen(command, cwd=self.project_dir, env=environment, stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        process_cleanup_pending = False
        try:
            output, _ = process.communicate(json.dumps({'prompt': prompt, 'session_id': session_id or None}, ensure_ascii=False).encode('utf-8'), timeout=budget)
        except subprocess.TimeoutExpired:
            cancel_file.touch()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                try:
                    if os.name == 'nt':
                        subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True,
                                       creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=10)
                    else:
                        process.kill()
                except (OSError, subprocess.TimeoutExpired):
                    process_cleanup_pending = True
            try:
                process.communicate(timeout=3)
            except subprocess.TimeoutExpired:
                process_cleanup_pending = True
            return {'ok': False, 'session_id': None,
                    **recover(self.runtime_dir,prior_runs,prompt,session_id,data_scope),
                    'process_cleanup_pending': process_cleanup_pending,
                    'error': '模型分析超时。银行已完成的操作仍以原回执为准。'
                             + (' 后台进程清理尚未确认，请勿重复办理。' if process_cleanup_pending else '')}
        finally:
            # A surviving adapter must still see the cancellation request.
            # Process exit/output drain alone does not certify Docker removal.
            if not process_cleanup_pending:
                cancel_file.unlink(missing_ok=True)
        try:
            result = json.loads(output.decode('utf-8'))
            if type(result) is not dict or type(result.get('ok')) is not bool:
                raise ValueError('Invalid result')
            if process.returncode != 0:
                result['ok'] = False
                result.setdefault('error', 'DSH 进程未正常完成。')
            return result
        except (ValueError, UnicodeError):
            return {'ok': False, **recover(self.runtime_dir,prior_runs,prompt,session_id,data_scope),
                    'error': 'DSH 未返回有效结果，请检查本地运行诊断；没有将此任务记为成功。'}


class Worker:
    def __init__(self, service, jobs, runner, *, clock, can_run=None):
        self.service, self.jobs, self.runner, self.clock = service, jobs, runner, clock
        self.stop_event = threading.Event()
        self.thread = None
        self.can_run = can_run

    def start(self):
        self.jobs.recover()
        if getattr(self.service, 'goal_followup', None):
            self.host_state('goal-followup.recover',{},lambda:self.service.goal_followup.recover(self.jobs))
        self.thread = threading.Thread(target=self.run, name='bank-v1-worker', daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()

    def host_state(self, entry, args, callback):
        if hasattr(self.service,'host_action'):
            return self.service.host_action(entry,args,callback,origin='SYSTEM')
        return callback()

    def check_due(self):
        settings = self.jobs.setting('schedule', {'enabled': True, 'cadence': 'daily'})
        if not settings['enabled']:
            return
        today = self.clock().astimezone(timezone(timedelta(hours=8))).date()
        last = self.jobs.setting('last_due_date')
        interval = 7 if settings['cadence'] == 'weekly' else 1
        if last is None or today.isoformat() >= (type(today).fromisoformat(last) + timedelta(days=interval)).isoformat():
            if hasattr(self.service,'handoff_store'):
                self.service.user_action('due_check', {'date': today.isoformat()},origin='SYSTEM')
            else:
                self.service.user_action('due_check', {'date': today.isoformat()})
            self.jobs.set_setting('last_due_date', today.isoformat())

    def enqueue_events(self):
        followup = getattr(self.service, 'goal_followup', None)
        if followup and followup.active():
            self.host_state('goal-followup.enqueue',{'observe':False},lambda:followup.enqueue(self.jobs, observe=False))
            return  # Bank facts remain intact; no synthetic event acknowledgement.
        for event in self.service.pending_events(limit=20):
            event_id, attempts = event['event_id'], event['attempts']
            generation = event['payload'].get('recovery_generation', 0)
            retry_key = 'event_retry:' + event_id
            retry_after = self.jobs.setting(retry_key)
            if retry_after and self.clock().isoformat() < retry_after:
                continue
            prompt = event_prompt(event_id)
            job = self.jobs.add(prompt, kind='event', event_key=f'event:{event_id}:{generation}:{attempts}', session_id='')
            # The process may have stopped between queue persistence and domain acknowledgement.
            if job['status'] in {'FAILED', 'INTERRUPTED', 'DONE'}:
                self.acknowledge_event(job)

    def acknowledge_event(self, job):
        if self.jobs.handoff and self.jobs.handoff.for_job(job['id']).source_contract == 'legacy_source_v1':
            return
        parts = job['event_key'].split(':')
        # Compatibility with jobs created before recovery generations were introduced.
        _, event_id, generation, attempt = parts if len(parts) == 4 else [parts[0], parts[1], '0', parts[2]]
        # Domain attempts are the durable acknowledgement, avoiding double failure counts.
        pending = {item['event_id']: item for item in self.service.pending_events(limit=100)}
        event = pending.get(event_id)
        if (event is None or event['attempts'] != int(attempt)
                or event['payload'].get('recovery_generation', 0) != int(generation)):
            return
        result = job['result'] or {}
        contexts = [item for item in result.get('trace', []) if isinstance(item, dict)
                    and item.get('kind') == 'response' and item.get('tool') == 'event_context'
                           and item.get('ok') is True and item.get('args', {}).get('event_id') == event_id
                    and isinstance(item.get('response'), dict)]
        refs = contexts[-1]['response'].get('data', {}).get('forecast', {}).get('evidence_refs') if contexts else None
        ok = result.get('ok') is True and bool(result.get('text')) and isinstance(refs, list) and bool(refs)
        try:
            self.service.finish_event(event_id, result.get('text', '') if ok else '', success=ok,
                                      error=None if ok else (failure_message(result) or '分析未读取对应银行事件，不能记为完成')[:500],
                                      expected_evidence_refs=refs if ok else None)
        except DomainError as error:
            if error.code != 'STALE_ANALYSIS':
                raise
            ok = False
            self.service.finish_event(event_id, '', success=False, error='分析期间账本或安排发生变化，原解释未采用，将重新读取。')
        if not ok:
            self.jobs.set_setting('event_retry:' + event_id, (self.clock() + timedelta(seconds=60)).isoformat())

    def step(self):
        # Leave queued requests and due-check scheduling intact while the local
        # environment/budget is unavailable. Do not spend an AI attempt on it.
        if self.can_run is not None and not self.can_run():
            return False
        self.check_due()
        self.enqueue_events()
        job = self.jobs.claim()
        if job is None:
            return False
        followup = getattr(self.service, 'goal_followup', None)
        if job['kind'] == 'event' and followup and followup.active():
            self.jobs.finish(job['id'], {'ok': False, 'error': '主动解释已统一进入目标检查；银行事件未伪标完成。'})
            return True
        try:
            scope = job.get('scope', 'bank')
            session_key = 'chat_session' if scope == 'bank' else 'moneydata_session'
            session_id = self.jobs.setting(session_key) if job['kind'] == 'chat' and not job.get('retry_of') else None
            # DSH resume appends to the existing session, even if this process
            # stops before a result is committed. Detach before running, and
            # restore only after an accepted response is durably saved.
            if scope == 'bank' and session_id:
                if not self.jobs.clear_setting_if_matches(session_key, session_id):
                    session_id = None
            if job['kind'] == 'goal':
                if followup is None or not followup.ai_enabled:
                    raise ValueError('Goal AI is disabled')
                check_id=job['event_key'].split(':',1)[1]
                self.host_state('goal-followup.start',{'check_id':check_id},lambda:followup.start(check_id))
            options = {'readonly': job['kind'] in {'event', 'goal'} or bool(job.get('read_only')) or scope == 'moneydata'}
            if scope != 'bank':
                options['data_scope'] = scope
            contextual = getattr(type(self.runner), 'run_request', None)
            if scope == 'bank' and job['kind'] == 'chat' and callable(contextual):
                result = self.runner.run_request(job['prompt'], session_id,
                    request_context=self.jobs.request_context(job['id']), readonly=options['readonly'])
            elif scope == 'bank' and job['kind'] in {'event','goal'} and hasattr(self.runner,'run_typed'):
                result = self.runner.run_typed(job['prompt'],self.jobs.handoff.for_job(job['id']))
            else:
                result = self.runner(job['prompt'], session_id, **options)
            if result.get('ok') and not result.get('text'):
                result = {'ok': False, 'error': '智能体没有返回分析内容。'}
            if scope == 'moneydata':
                result = validate_history_answer(result, self.jobs.original_prompt(job['id']))
        except Exception:
            result = {'ok': False, 'error': '智能体运行失败，请检查本地服务。银行账本未回滚或自动重放。'}
        if job['kind'] == 'goal' and followup:
            check_id=job['event_key'].split(':',1)[1]
            result = self.host_state('goal-followup.finish',{'check_id':check_id,'model_ok':bool(result.get('ok'))},lambda:followup.finish(check_id, result))
        else:
            from .display_check import check
            result = check(result, self.service)
        self.jobs.finish(job['id'], result)
        if self.jobs.handoff:
            self.jobs.handoff.producers_finished(self.jobs.handoff.for_job(job['id']),trace_finalized=not result.get('process_cleanup_pending'))
        if job['kind'] == 'event':
            self.acknowledge_event(self.jobs.get(job['id']))
        elif job['kind'] == 'chat' and result.get('ok') and result.get('session_id') and not job.get('retry_of'):
            if scope == 'bank' and self.jobs.handoff:
                self.jobs.save_session(job['id'],result)
            else:
                self.jobs.set_setting(session_key, result['session_id'])
        return True

    def run(self):
        while not self.stop_event.is_set():
            try:
                worked = self.step()
                self.jobs.set_setting('worker_error', None)
            except Exception as error:
                # Keep durable state available when an isolated iteration fails.
                self.jobs.set_setting('worker_error', type(error).__name__)
                worked = False
            if not worked:
                self.stop_event.wait(2)
