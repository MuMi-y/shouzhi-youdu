"""Deterministic candidates and user-confirmed transaction/plan reconciliation."""
from datetime import date
import json
from .common import DomainError, canonical
from .storage import audit


def latest_plans(db):
    return [dict(row) for row in db.execute('''SELECT a.* FROM arrangements a JOIN
        (SELECT arrangement_id,MAX(version) v FROM arrangements GROUP BY arrangement_id) b
        ON a.arrangement_id=b.arrangement_id AND a.version=b.v''')]


def matches(db):
    return [dict(row) for row in db.execute("SELECT * FROM arrangement_matches ORDER BY confirmed_at,transaction_id")]


def candidates(db):
    linked = matches(db)
    linked_transactions = {row["transaction_id"] for row in linked}
    linked_plans = {row["arrangement_id"] for row in linked}
    rejected = {tuple(row) for row in db.execute("SELECT transaction_id,arrangement_id,expected_version FROM match_rejections")}
    rows = [dict(row) for row in db.execute('''SELECT t.* FROM transactions t JOIN accounts a USING(account_id)
        WHERE a.subject_id='U1' AND t.kind IN ('income','expense')
        AND NOT EXISTS(SELECT 1 FROM transactions r WHERE r.kind='refund' AND r.original_transaction_id=t.transaction_id)
        ORDER BY t.business_date DESC,t.transaction_id''')]
    plans = [plan for plan in latest_plans(db) if plan["status"] == "active" and plan["arrangement_id"] not in linked_plans]
    result = []
    for tx in rows:
        if tx["transaction_id"] in linked_transactions:
            continue
        for plan in plans:
            identity = (tx["transaction_id"], plan["arrangement_id"], plan["version"])
            distance = abs((date.fromisoformat(tx["business_date"])-date.fromisoformat(plan["due_date"])).days)
            if identity in rejected or tx["kind"] != plan["kind"] or abs(tx["amount_minor"]) != plan["amount_minor"] or distance > 14:
                continue
            result.append({"candidate_id": ":".join(map(str,identity)), "transaction_id":tx["transaction_id"],
                "arrangement_id":plan["arrangement_id"], "expected_version":plan["version"],
                "title":plan["title"], "kind":plan["kind"], "amount_minor":plan["amount_minor"],
                "transaction_date":tx["business_date"], "due_date":plan["due_date"], "date_distance_days":distance,
                "reasons":["收支方向相同", "金额完全相同", f"交易日与安排日相距 {distance} 天（匹配窗口14天）"],
                "notice":"只是可能对应，不能凭同额认定。确认后该安排标记完成，不再纳入未来预测；不会再次记账。"})
    for item in result:
        item["ambiguous"] = sum(other["transaction_id"] == item["transaction_id"] or other["arrangement_id"] == item["arrangement_id"] for other in result) > 1
    return result


def selected_candidate(db, args):
    plan = next((plan for plan in latest_plans(db) if plan["arrangement_id"] == args["arrangement_id"]), None)
    if not plan:
        raise DomainError("NOT_FOUND", "安排不存在。")
    if plan["version"] != args["expected_version"]:
        raise DomainError("VERSION_CONFLICT", "安排版本已变化，请重新核对关联。")
    selected = next((item for item in candidates(db) if item["transaction_id"] == args["transaction_id"] and item["arrangement_id"] == args["arrangement_id"]), None)
    if selected is None:
        raise DomainError("MATCH_UNAVAILABLE", "此流水与安排不再是可确认候选，可能已关联、已拒绝或不符合匹配条件。")
    return selected


def confirmation_params(candidate):
    return {name:candidate[name] for name in ("transaction_id", "arrangement_id", "expected_version", "title", "kind", "amount_minor", "transaction_date", "due_date")}


def validate_confirmation(db, params):
    candidate = selected_candidate(db, params)
    if canonical(params) != canonical(confirmation_params(candidate)):
        raise DomainError("MATCH_CHANGED", "关联依据已变化，请重新生成确认摘要。")
    return candidate


def prepare(db, args, now):
    """Keep retries stable even after a successful confirmation consumes the candidate."""
    from .operations import get_operation, prepare as prepare_operation
    old = db.execute("SELECT * FROM operations WHERE idempotency_key=?",(args["idempotency_key"],)).fetchone()
    if old:
        params = json.loads(old["params_json"])
        if old["kind"] != "arrangement_match" or any(params.get(key) != args[key] for key in ("transaction_id","arrangement_id","expected_version")):
            raise DomainError("IDEMPOTENCY_CONFLICT", "相同请求编号不能更换关联。")
        return get_operation(db,old["operation_id"],now)
    candidate = selected_candidate(db,args)
    return prepare_operation(db,"arrangement_match",{**confirmation_params(candidate),"idempotency_key":args["idempotency_key"]},now)


def apply_match(db, params, operation_id, now):
    candidate = validate_confirmation(db, params)
    version = candidate["expected_version"]+1
    db.execute('''INSERT INTO arrangements(arrangement_id,version,title,kind,amount_minor,due_date,created_at,source,status)
        VALUES(?,?,?,?,?,?,?,?,'completed')''', (candidate["arrangement_id"],version,candidate["title"],candidate["kind"],
        candidate["amount_minor"],candidate["due_date"],now.isoformat(),"USER_CONFIRMED_TRANSACTION_MATCH"))
    result = {"transaction_id":candidate["transaction_id"], "arrangement_id":candidate["arrangement_id"],
        "arrangement_version":candidate["expected_version"], "completed_version":version,
        "operation_id":operation_id, "confirmed_at":now.isoformat()}
    db.execute("INSERT INTO arrangement_matches VALUES(?,?,?,?,?,?)", tuple(result.values()))
    return {**result, "notice":"已关联原流水并标记安排完成；没有再次记账。"}


def reject_candidate(db, args, now):
    from . import projection_journal
    key = (args["transaction_id"],args["arrangement_id"],args["expected_version"])
    if not db.execute("SELECT 1 FROM match_rejections WHERE transaction_id=? AND arrangement_id=? AND expected_version=?",key).fetchone():
        selected_candidate(db, args)
        before = projection_journal.capture(db, now)
        db.execute("INSERT INTO match_rejections VALUES(?,?,?,?)",(*key,now.isoformat()))
        audit(db,"MATCH_REJECTED",args["transaction_id"],now,args)
        projection_journal.append(db, before, 'match-reject:' + ':'.join(map(str, key)),
            {'kind': 'match_rejected', 'transaction_ids': [args['transaction_id']],
             'arrangement_id': args['arrangement_id']}, now)
    return {"status":"REJECTED",**args}
