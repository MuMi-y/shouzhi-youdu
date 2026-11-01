"""Bounded DAG and tool argument validation, without executable expressions."""
import copy
import re
from jsonschema import Draft202012Validator, FormatChecker


class TaskError(Exception):
    def __init__(self, code, message):
        self.code, self.message = code, message
        super().__init__(message)


ALLOWED = frozenset({"accounts", "transactions", "spending_summary", "arrangements", "forecast",
    "operation", "event_context", "prepare_transfer", "prepare_mandate_query", "prepare_mandate_cancel",
    "match_mandate_from_query", "propose_arrangement", "billing_report", "recurring_candidates", "insight_status", "check_insights", "money_calculate"})
PREPARES = frozenset({"prepare_transfer", "prepare_mandate_query", "prepare_mandate_cancel", "propose_arrangement"})


def require(condition, message, code="INVALID_PLAN"):
    if not condition:
        raise TaskError(code, message)


def references(value):
    if isinstance(value, str):
        # A serialized reference is not a literal operation ID. Reject at plan
        # admission, before any predecessor can create a bank operation.
        require(not re.match(r'''^\s*\{\s*["']?\$ref["']?\s*:''', value),
                '结果引用须为JSON对象 {"$ref":"节点.data.字段"}，不能是包含该对象的字符串。',
                "INVALID_REFERENCE")
    if isinstance(value, dict):
        if "$ref" in value:
            require(set(value) == {"$ref"} and isinstance(value["$ref"], str), "引用必须为唯一$ref字符串。")
            parts = value["$ref"].split(".")
            require(len(parts) >= 2 and all(re.fullmatch(r"[A-Za-z0-9_]+", p) for p in parts), "引用路径无效。")
            return [parts]
        return [p for item in value.values() for p in references(item)]
    if isinstance(value, list):
        return [p for item in value for p in references(item)]
    return []


def arguments(args, schema, *, deferred=False):
    # Validate all literal siblings even when one leaf is a deferred result.
    if deferred and isinstance(args, dict) and set(args) == {"$ref"}:
        return
    if deferred and references(args):
        require(isinstance(args, dict) and schema.get("type") == "object", "仅对象参数允许结果引用。")
        require(not (set(schema.get("required", [])) - set(args)), "缺少必要工具参数。")
        props = schema.get("properties", {})
        require(schema.get("additionalProperties", True) is not False or not(set(args)-set(props)), "包含禁止工具参数。")
        for key, value in args.items():
            arguments(value, props.get(key, {}), deferred=True)
        return
    errors = list(Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(args))
    require(not errors, "工具参数类型、范围或格式无效。")


def validate_plan(plan, specs):
    require(type(plan) is dict and set(plan) == {"title", "nodes"}, "计划须包含title和nodes。")
    require(type(plan["title"]) is str and 1 <= len(plan["title"]) <= 200, "任务标题长度无效。")
    nodes = copy.deepcopy(plan["nodes"])
    require(type(nodes) is list and 1 <= len(nodes) <= 20, "计划须包含1至20个节点。")
    identifiers = set()
    for node in nodes:
        require(type(node) is dict and set(node) == {"id", "tool", "args", "depends_on"}, "节点字段无效。")
        require(type(node["id"]) is str and bool(re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{0,39}", node["id"])), "节点ID无效。")
        require(node["id"] not in identifiers, "节点ID重复。")
        identifiers.add(node["id"])
        require(type(node["tool"]) is str and node["tool"] in ALLOWED and node["tool"] in specs, "工具不在银行任务白名单。")
        require(type(node["args"]) is dict, "args须为对象。")
        require(type(node["depends_on"]) is list and all(type(x) is str for x in node["depends_on"]), "依赖须为ID列表。")
        require(len(node["depends_on"]) == len(set(node["depends_on"])), "依赖重复。")
        args = dict(node["args"])
        if node["tool"] in PREPARES:
            args["idempotency_key"] = "task_reserved_key"
        arguments(args, specs[node["tool"]]["parameters"], deferred=True)
    lookup = {n["id"]: n for n in nodes}
    ancestors = {}
    def visit(identifier, visiting):
        require(identifier in lookup, "依赖节点不存在。")
        require(identifier not in visiting, "计划存在循环依赖。")
        if identifier in ancestors:
            return ancestors[identifier]
        result = set()
        for parent in lookup[identifier]["depends_on"]:
            result.add(parent)
            result.update(visit(parent, visiting | {identifier}))
        ancestors[identifier] = result
        return result
    for node in nodes:
        allowed = visit(node["id"], set())
        for ref in references(node["args"]):
            require(ref[0] in allowed, "结果引用必须指向明确依赖的祖先节点。")
    validate_mandate_dependencies(nodes, ancestors)
    return nodes


def validate_mandate_dependencies(nodes, ancestors):
    """New matcher plans bind ID, version and receipt from one explicit result."""
    matchers = {n["id"] for n in nodes if n["tool"] == "match_mandate_from_query"}
    if not matchers:
        return  # Existing plans and direct API calls retain their contract.
    for node in nodes:
        if node["tool"] != "prepare_mandate_cancel":
            continue
        binding = node["args"].get("mandate_id")
        require(type(binding) is dict and set(binding) == {"$ref"},
                "匹配计划的取消对象须引用唯一匹配结果。")
        matcher = binding["$ref"].split(".")[0]
        require(matcher in matchers and matcher in ancestors[node["id"]],
                "取消必须依赖对应的唯一匹配节点。")
        paths = {"mandate_id":"selected.mandate_id", "expected_version":"selected.version",
                 "query_operation_id":"query_operation_id"}
        for argument, path in paths.items():
            require(node["args"].get(argument) == {"$ref":f"{matcher}.data.match.{path}"},
                    "取消的对象、版本及查询来源必须引用同一匹配节点。")


def resolve(value, results):
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            parts = value["$ref"].split(".")
            try:
                item = results[parts[0]]
                for key in parts[1:]:
                    item = item[int(key)] if isinstance(item, list) else item[key]
                return copy.deepcopy(item)
            except (KeyError, IndexError, ValueError, TypeError):
                raise TaskError("MISSING_REFERENCE", "上游结果缺少计划引用的数据，请澄清或重新规划。") from None
        return {k: resolve(v, results) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve(v, results) for v in value]
    return value
