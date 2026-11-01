"""Host-created single-run capabilities around existing tools; no model keys."""
from copy import deepcopy
from dataclasses import dataclass, field
import secrets
import threading
import time

from jsonschema import Draft202012Validator, ValidationError, FormatChecker

from src.bank_v1.common import DomainError
from .policy import BINDING_TOOLS, reject_unless
from .schema import binding_schema


READ_ONLY = frozenset({'accounts', 'transactions', 'spending_summary', 'arrangements', 'forecast',
    'operation', 'event_context', 'billing_report', 'recurring_candidates', 'insight_status',
    'task_status', 'money_calculate', 'goal_context', 'compare_goal_options', 'preview_goal_options',
    'compare_goal_impact', 'explain_goal_changes'})


@dataclass
class RequestLease:
    context: dict | None
    readonly: bool
    deadline: float
    handle: object = None
    records: list = field(default_factory=list)
    lock: object = field(default_factory=threading.RLock)


class RequestGateway:
    def __init__(self, *, policy, call_tool, tool_specs, monotonic=time.monotonic, store=None):
        self.policy, self.call_tool, self.tool_specs = policy, call_tool, tool_specs
        self.monotonic = monotonic
        self.store = store
        self._leases = {}
        self._lock = threading.RLock()

    def open(self, context, *, readonly, lifetime=240, handle=None):
        reject_unless(0 < lifetime <= 600, 'INVALID_REQUEST_LIFETIME', '请求有效期无效。')
        if self.store:
            self.store.validate_handle(handle)
            reject_unless(handle.read_only == bool(readonly), 'REQUEST_IDENTITY_MISMATCH', '只读权限不匹配。')
            if context is not None:
                reject_unless(context.get('run_id') == handle.run_id and context.get('source_contract') == handle.source_contract,
                              'REQUEST_IDENTITY_MISMATCH', '宿主请求身份不匹配。')
        with self._lock:
            now = self.monotonic()
            self._leases = {key: lease for key, lease in self._leases.items() if lease.deadline > now}
            reject_unless(len(self._leases) < 8, 'REQUEST_LIMIT', '当前请求过多，请等待已有请求结束。')
            token = secrets.token_urlsafe(32)
            self._leases[token] = RequestLease(deepcopy(context), bool(readonly), now + lifetime, handle)
            return token

    def recognizes(self, token):
        with self._lock:
            lease = self._leases.get(token)
            return lease is not None and lease.deadline > self.monotonic()

    def _lease(self, token):
        with self._lock:
            lease = self._leases.get(token)
            reject_unless(lease is not None and lease.deadline > self.monotonic(),
                          'REQUEST_EXPIRED', '本轮工具权限已结束，不能重放调用。')
            return lease

    def close(self, token):
        with self._lock:
            lease = self._leases.pop(token, None)
        if lease and lease.lock.acquire(timeout=2):
            lease.lock.release()
            return True
        return lease is None

    def validates(self, token, handle):
        lease = self._lease(token)
        return lease.handle == handle

    def specs(self, token):
        lease = self._lease(token)
        result = deepcopy(self.tool_specs())
        if lease.readonly:
            result = [spec for spec in result if spec['name'] in READ_ONLY]
        if lease.context is not None:
            for spec in result:
                if spec['name'] not in BINDING_TOOLS:
                    continue
                spec['parameters']['properties']['request_binding'] = binding_schema(lease.handle.source_contract if lease.handle else 'legacy_source_v1')
                spec['parameters']['required'].append('request_binding')
                spec['description'] += ' 调用前须提供request_binding；沿用宿主原话和当前动作，不能把工具事实冒充用户条件。'
        return result

    def call(self, token, name, args):
        lease = self._lease(token)
        with lease.lock:
            reject_unless(self.recognizes(token), 'REQUEST_EXPIRED', '本轮工具权限已结束。')
            attempt = self.store.begin_attempt(lease.handle,name,args,'MODEL_GATEWAY',effect_class=(
                'BANK_PREPARE' if name in {'prepare_transfer','prepare_mandate_query','prepare_mandate_cancel','propose_arrangement'}
                else 'TASK_WRITE' if name in {'submit_task_plan','replan_task'} else 'GOAL_DRAFT' if name == 'propose_goal'
                else 'INSIGHT_WRITE' if name == 'check_insights' else 'READ_ONLY' if name in READ_ONLY else 'UNKNOWN_TOOL')) if self.store else None
            dispatched = False
            domain_failure = None
            params = deepcopy(args)
            try:
                reject_unless(type(args) is dict, 'INVALID_ARGUMENTS', '工具参数须为对象。')
                reject_unless(not lease.readonly or name in READ_ONLY, 'REQUEST_READ_ONLY', '本轮仅允许查询与计算。')
                reject_unless(len(lease.records) < 64, 'REQUEST_TOOL_LIMIT', '本轮工具调用已到上限。')
                definition = next((spec for spec in self.specs(token) if spec['name'] == name), None)
                reject_unless(definition is not None, 'UNKNOWN_TOOL', '本轮没有这个可用工具。')
                try:
                    Draft202012Validator(definition['parameters'], format_checker=FormatChecker()).validate(args)
                except ValidationError:
                    raise DomainError('INVALID_ARGUMENTS', '工具参数缺失、格式错误或包含禁止字段；尚未调用业务工具。') from None
                contract = None
                if lease.context is not None and name in BINDING_TOOLS:
                    binding = params.pop('request_binding', None)
                    contract = self.policy.bind(binding, lease.context, lease.records)
                    if name in {'submit_task_plan', 'replan_task'}:
                        self.policy.check_plan(params.get('nodes', []), contract)
                    else:
                        params = self.policy.check_call(name, params, self.policy.item_for(contract))
                else:
                    reject_unless('request_binding' not in params, 'INVALID_ARGUMENTS', '此入口不接受业务绑定字段。')
                if attempt:
                    from src.bank_v1_web.handoff_contracts import digest
                    self.store.validated(attempt,params,binding_hash=digest(contract) if contract is not None else None)
                    self.store.append(attempt,'DISPATCHED',{})
                    dispatched = True
                if name in {'compare_goal_impact', 'explain_goal_changes'} and lease.context is not None:
                    body = self.call_tool(name, params, impact_context=lease.context.get('goal_impact_context'))
                elif contract is not None and name in {'submit_task_plan', 'replan_task'}:
                    body = self.call_tool(name, params, request_contract=contract)
                else:
                    try:
                        body = self.call_tool(name, params)
                    except DomainError as error:
                        from src.bank_v1_web.handoff_read_failure import receipt
                        domain_failure = receipt(name, params, error)
                        raise
                lease.records.append({'tool': name, 'args': deepcopy(params), 'ok': True, 'status': 200, 'response': deepcopy(body)})
                if attempt:
                    from src.bank_v1_web.handoff_contracts import object_refs, digest, safe
                    self.store.append(attempt,'RETURNED',{'refs':object_refs(body),'response_hash':digest(safe(body)),
                        **({'contract_hash':digest(contract)} if contract is not None else {})})
                return body
            except BaseException as error:
                lease.records.append({'tool': name, 'args': deepcopy(params), 'ok': False, 'status': 409,
                    'response': {'error': {'code': getattr(error,'code',type(error).__name__), 'message': error.message if isinstance(error,DomainError) and hasattr(error, 'message') else '工具请求未完成。'}}})
                if attempt:
                    try:
                        self.store.append(attempt,'RAISED' if dispatched else 'REJECTED_BEFORE_DISPATCH',
                                          domain_failure or {'code':getattr(error,'code',type(error).__name__)})
                    except Exception:
                        self.store.mark_incomplete(lease.handle.run_id)
                raise
