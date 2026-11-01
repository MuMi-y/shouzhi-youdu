"""Compose fixed bank, insight and task services without expanding bank authority."""
from pathlib import Path

from jsonschema import Draft202012Validator, ValidationError

from src.bank_v1 import DomainError
from src.bank_v1.common import evidence
from src.bank_tasks import TaskService
from src.bank_tasks.progress import build as task_progress
from src.bank_insights import InsightService
from src.bank_goals import GoalService
from src.bank_goals.tools import GoalTools, tool_specs as goal_specs
from src.bank_goals.followup import GoalFollowup
from .money_calculate import CalculationError, calculate, tool_spec as calculation_spec
from src.bank_evidence.facts import attach as attach_facts, SUPPORTED_TOOLS
from src.bank_evidence.calculation import SOURCE_KEYS, calculate_bound, source_schema
from src.bank_evidence.calculation_store import CalculationStore
from src.bank_requests.policy import RequestPolicy
from src.bank_requests.gateway import RequestGateway


def sourced_calculation_spec():
    definition = calculation_spec()
    definition['parameters']['properties'].update({key: source_schema() for key in sorted(SOURCE_KEYS)})
    definition['parameters']['dependentRequired'] = {
        'left_source': ['right_source'], 'right_source': ['left_source']}
    definition['description'] += (' 银行金额计算应同时提供left_source/right_source：'
        'bank_fact用查询返回fact_catalog中的fact_id及原tool/arguments，服务会重新取证；'
        'user_assumption.quote须逐字引用用户原话且只包含一个明确元金额。'
        '连续计算可用calculation_result和本实例已返回的evidence_id引用前项金额，最多四层；'
        '系统会重查全部根事实，祖先假设仍须匹配当前用户原话。百分比不得当金额。'
        '两项来源均须提供，currency=CNY；事实变化会拒绝旧引用。')
    return definition


def text(maximum=300):
    return {'type': 'string', 'minLength': 1, 'maxLength': maximum}


def spec(name, description, properties, required):
    return {'name': name, 'description': description, 'parameters': {
        'type': 'object', 'properties': properties, 'required': required,
        'additionalProperties': False}}


def task_specs():
    node = {'type': 'object', 'properties': {
        'id': {**text(40), 'pattern': '^[A-Za-z][A-Za-z0-9_]*$'},
        'tool': text(80), 'args': {'type': 'object'},
        'depends_on': {'type': 'array', 'items': text(40), 'uniqueItems': True}},
        'required': ['id', 'tool', 'args', 'depends_on'], 'additionalProperties': False}
    nodes = {'type': 'array', 'items': node, 'minItems': 1, 'maxItems': 20}
    return [
        spec('submit_task_plan', '保存涵盖用户要求各步的持久任务图，包括前序只读核对和依赖，不能只保存最后一步。跨节点参数必须用JSON对象，例如{"operation_id":{"$ref":"prepare_transfer_node.data.operation.operation_id"}}，不能把引用对象写成字符串。准备银行操作会等待本人确认；仅准备节点args内的幂等键由执行器补全，顶层任务请求idempotency_key必须提供。计划保存不等于执行成功，后续用task_status核实。',
             {'prompt': text(6000), 'title': text(200), 'nodes': nodes, 'idempotency_key': text(160)},
             ['prompt', 'title', 'nodes', 'idempotency_key']),
        spec('task_status', '查询任务图、节点状态、工具结果及replan_guidance快照。FAILED不等于禁止修改；CONDITIONAL仅表示状态/版本未禁止，须核对具体约束。只有所有节点SUCCEEDED才能说任务全部完成；银行已成功但核对节点FAILED时仍需修复或明确尚未收尾。',
             {'task_id': text(100)}, ['task_id']),
        spec('replan_task', '按task_status的replan_guidance与当前版本修复未完成/FAILED节点。仅SUCCEEDED/CANCELLED状态禁止重规划；FAILED可修但不保证通过。计划版本必须小于10，成功节点原定义不可改删，不能重放交易。改动节点的原操作须本人拒绝或已过期；未知提交先用原幂等键对账，实际已成功先恢复核回执。PAUSED/NEEDS_HUMAN不自动推进但可有条件重规划，成功后恢复可推进状态。新计划仍受DAG/参数/银行守卫限制；更新后读取task_status核实。',
             {'task_id': text(100), 'expected_version': {'type': 'integer', 'minimum': 1},
              'nodes': nodes, 'reason': text(500)}, ['task_id', 'expected_version', 'nodes', 'reason'])]


class CompetitionService:
    def __init__(self, directory, *, bank, clock, goals_enabled=False, goal_followup=False, goal_ai=False, handoff_store=None):
        self.bank, self.clock = bank, clock
        self.handoff_store = handoff_store
        if handoff_store:
            handoff_store.bind_bank(bank.db_path)
        self.db_path = bank.db_path
        directory = Path(directory)
        if goal_ai and not goal_followup or goal_followup and not goals_enabled:
            raise ValueError('Goal AI requires followup, and followup requires goals.')
        self.goals = GoalService(directory / 'goals.sqlite3', bank=bank, clock=clock) if goals_enabled else None
        self.goal_tools = GoalTools(self.goals) if goal_followup else None
        self.goal_followup = GoalFollowup(self.goal_tools, ai_enabled=goal_ai) if goal_followup else None
        self.insights = InsightService(directory / 'insights.sqlite3', bank=bank, clock=clock)
        self.calculations = CalculationStore(directory / 'calculations.sqlite3')
        self.request_policy = RequestPolicy(read_tool=self.business_tool, clock=clock)
        self.tasks = TaskService(directory / 'tasks.sqlite3', clock=clock,
                                 tool_specs=self.business_specs, call_tool=self.business_tool,
                                 before_call=self.request_policy.before_task_call,attempt_recorder=handoff_store,
                                 recover_prepare=self._recover_task_prepare if handoff_store else None)
        self.request_gateway = RequestGateway(policy=self.request_policy, call_tool=self.call_tool, tool_specs=self.tool_specs, store=handoff_store)
        if handoff_store:
            from src.bank_v1_web.handoff_reconcile import bind
            bind(handoff_store,self)

    def _recover_task_prepare(self, task, node, expected_args):
        from src.bank_v1_web.handoff_reconcile import recover_task_prepare
        return recover_task_prepare(self.handoff_store,self,task,node,expected_args)

    def business_specs(self):
        return self.bank.tool_specs() + self.insights.tool_specs() + [sourced_calculation_spec()] + (goal_specs() if self.goal_tools else [])

    def read_fact_source(self, name, args):
        if name not in SUPPORTED_TOOLS:
            raise DomainError('UNSUPPORTED_FACT_SOURCE', '只接受明确支持的只读来源。')
        if name == 'billing_report':
            return self.insights.call_tool(name, args)
        return self.bank.call_tool(name, args)

    def business_tool(self, name, args):
        if self.goal_tools and name in {s['name'] for s in goal_specs()}:
            return self.goal_tools.call(name, args)
        if name == "money_calculate":
            try:
                if type(args) is dict and SOURCE_KEYS & args.keys():
                    result = calculate_bound(args, self.read_fact_source, self.calculations.read)
                    self.calculations.save(result)
                    return result
                return calculate(args)
            except CalculationError as error:
                raise DomainError(error.code, str(error)) from None
        if name in {s['name'] for s in self.insights.tool_specs()}:
            return attach_facts(name, args, self.insights.call_tool(name, args))
        return attach_facts(name, args, self.bank.call_tool(name, args))

    def tool_specs(self):
        return self.business_specs() + task_specs()

    def call_tool(self, name, args, *, request_contract=None, impact_context=None):
        if self.handoff_store and request_contract is None and name in {'submit_task_plan','replan_task'}:
            return self._trusted_task_call(name,args)
        if name in {'compare_goal_impact', 'explain_goal_changes'} and self.goal_tools:
            return self.goal_tools.call(name, args, impact_context=impact_context)
        definition = next((s for s in task_specs() if s['name'] == name), None)
        if definition is None:
            return self.business_tool(name, args)
        try:
            Draft202012Validator(definition['parameters']).validate(args)
        except ValidationError:
            raise DomainError('INVALID_ARGUMENTS', '任务参数缺失、类型错误或包含禁止字段。') from None
        if name == 'submit_task_plan':
            result = self.tasks.submit(args['prompt'], {'title': args['title'], 'nodes': args['nodes']}, args['idempotency_key'], request_contract=request_contract)
        elif name == 'task_status':
            result = self.tasks.get(args['task_id'])
        else:
            if request_contract is None:
                existing = self.tasks.get(args['task_id']).get('request_contract')
                if existing is not None:
                    self.request_policy.check_plan(args['nodes'], existing)
            result = self.tasks.replan(args['task_id'], args['expected_version'], args['nodes'], args['reason'], request_contract=request_contract)
        now = self.clock()
        data = {'task': result}
        if name == 'task_status':
            identifiers = {n['operation_id'] for n in result['nodes'] if n.get('operation_id')}
            operations = {identifier: self.bank.call_tool('operation', {'operation_id': identifier})['data']['operation']
                          for identifier in sorted(identifiers)}
            data['task_progress'] = task_progress(result, operations, now.isoformat())
        return evidence(data, now)

    def _trusted_task_call(self, name, args):
        """Existing root/domain entry, never issued to the competition model."""
        store=self.handoff_store
        handle=store.open_host_run('SYSTEM',{'entry':name,'channel':'TRUSTED_ROOT'})
        attempt=store.begin_attempt(handle,name,args,'SYSTEM',effect_class='TASK_WRITE')
        dispatched=False
        try:
            contract=None
            if name=='submit_task_plan' and isinstance(args,dict):
                from src.bank_v1_web.handoff_reconcile import read
                import json
                prior=read(self.tasks.path,'SELECT document FROM tasks WHERE request_key=?',(args.get('idempotency_key'),))
                if prior:
                    contract=json.loads(prior['document']).get('request_contract')
            if name=='replan_task' and isinstance(args,dict) and 'task_id' in args:
                contract=self.tasks.get(args['task_id']).get('request_contract')
                if contract and contract.get('version')==1:
                    self.request_policy.check_plan(args.get('nodes',[]),contract)
            contract=contract or {'version':0,'origin_kind':'TRUSTED_ROOT','source_contract':'not_applicable',
                'run_id':handle.run_id,'request_id':handle.run_id}
            from src.bank_v1_web.handoff_contracts import digest
            store.validated(attempt,args,binding_hash=digest(contract));store.append(attempt,'DISPATCHED',{});dispatched=True
            result=self.call_tool(name,args,request_contract=contract)
            from src.bank_v1_web.handoff_contracts import object_refs,digest
            store.append(attempt,'RETURNED',{'refs':object_refs(result),'contract_hash':digest(contract)})
            store.close(handle,{'ok':True});store.producers_finished(handle)
            return result
        except BaseException as error:
            store.append(attempt,'RAISED' if dispatched else 'REJECTED_BEFORE_DISPATCH',{'code':getattr(error,'code',type(error).__name__)})
            store.close(handle,{'ok':False});store.producers_finished(handle)
            raise

    def host_action(self, entry, args, callback, *, origin='USER_ACTION', effect_class='SERVICE_STATE'):
        if self.handoff_store:
            return self.handoff_store.invoke(origin,entry,args,callback,effect_class=effect_class)
        return callback()

    def user_action(self, name, args, *, origin='USER_ACTION'):
        return self.host_action(name,args,lambda:self._user_action(name,args),origin=origin,effect_class='USER_BANK_ACTION')

    def _user_action(self, name, args):
        if name == 'inject_transaction' and self.goal_followup and self.goal_followup.active():
            # Capture the current goal/plan before a user posts money, even if the
            # periodic pump has not observed a just-edited plan. No model is run.
            self.goal_followup.observe(periodic_due=False)
        return self.bank.user_action(name, args)

    def state(self):
        value = self.bank.state()
        value['competition'] = {'edition': 'competition-v1', 'tasks': self.tasks.list(limit=20),
                                'insights': self.insights.call_tool('insight_status', {})}
        if self.goals is not None:
            value['competition']['goals'] = self.goals.state()
            if self.goal_followup:
                value['competition']['goals']['followup'] = self.goal_followup.state()
                value['competition']['goals']['drafts'] = self.goal_tools.drafts()
        return value

    def pending_events(self, limit=20):
        return self.bank.pending_events(limit)

    def finish_event(self, event_id, analysis_text, **kwargs):
        return self.host_action('finish_event',{'event_id':event_id,'success':kwargs.get('success')},
            lambda:self.bank.finish_event(event_id, analysis_text, **kwargs),origin='SYSTEM')

    def advance_tasks(self):
        for task in self.tasks.list(limit=20, active_only=True):
            if task['state'] in ('READY', 'WAITING_CONFIRMATION', 'WAITING_VERIFICATION'):
                self.tasks.tick(task['task_id'])

