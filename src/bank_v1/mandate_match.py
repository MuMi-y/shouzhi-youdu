"""Match only a confirmed query snapshot; never guess an array position."""
from datetime import datetime, timedelta
import json
import unicodedata
from .common import DomainError, business_date


def require(condition, code, message):
    if not condition:
        raise DomainError(code, message)


def normalize(text):
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def snapshot(row):
    """The complete mandate record that the bank actually exposes in queries."""
    return {key:row[key] for key in
            ("mandate_id", "subject_id", "name", "amount_minor", "status", "version")}


def valid_item(item):
    return (type(item) is dict
            and all(type(item.get(k)) is str and item[k] for k in
                    ("mandate_id", "subject_id", "name", "status"))
            and type(item.get("version")) is int and item["version"] >= 1
            and type(item.get("amount_minor")) is int and item["amount_minor"] >= 0
            and item["status"] in {"ACTIVE", "CANCELLED"})


def query_receipt(db, operation_id, now):
    row = db.execute("SELECT * FROM operations WHERE operation_id=?", (operation_id,)).fetchone()
    require(row is not None, "INVALID_QUERY_RECEIPT", "查询操作不存在或未成功。")
    require(row["subject_id"] == "U1", "FORBIDDEN", "无权读取该查询回执。")
    require(row["kind"] == "mandate_query" and row["status"] == "SUCCEEDED",
            "INVALID_QUERY_RECEIPT", "必须先确认正式查询并取得成功回执。")
    try:
        receipt = json.loads(row["receipt_json"])
        require(type(receipt) is dict, "INVALID_QUERY_RECEIPT", "查询回执结构无效。")
        require(receipt.get("operation_id") == operation_id
                and receipt.get("kind") == "mandate_query" and receipt.get("status") == "SUCCEEDED"
                and type(receipt.get("receipt_id")) is str and bool(receipt["receipt_id"]),
                "INVALID_QUERY_RECEIPT", "查询回执身份或状态不一致。")
        completed = datetime.fromisoformat(receipt["committed_at"])
        created = datetime.fromisoformat(row["created_at"])
        require(completed.tzinfo is not None and completed.utcoffset() is not None,
                "INVALID_QUERY_RECEIPT", "查询回执时间缺少时区。")
        require(completed >= created and receipt.get("business_date") == business_date(completed),
                "INVALID_QUERY_RECEIPT", "查询回执日期不一致。")
        require(timedelta(0) <= now - completed <= timedelta(minutes=30)
                and business_date(completed) == business_date(now),
                "QUERY_RECEIPT_STALE", "查询回执不在同业务日有效30分钟内。")
        items = receipt["mandates"]
        require(type(items) is list and all(valid_item(item) for item in items),
                "INVALID_QUERY_RECEIPT", "查询回执的授权字段不完整。")
        require(all(item["subject_id"] == "U1" for item in items),
                "FORBIDDEN", "查询回执含非本人授权。")
        require(len({item["mandate_id"] for item in items}) == len(items),
                "INVALID_QUERY_RECEIPT", "查询回执授权编号重复。")
    except (KeyError, TypeError, ValueError):
        raise DomainError("INVALID_QUERY_RECEIPT", "查询回执字段或时间无效。") from None
    return receipt


def current_snapshot(db, mandate_id):
    row = db.execute("SELECT * FROM mandates WHERE mandate_id=?", (mandate_id,)).fetchone()
    require(row is not None, "VERSION_CONFLICT", "原授权已不存在，需重新查询。")
    require(row["subject_id"] == "U1", "FORBIDDEN", "无权访问此授权。")
    require(row["status"] == "ACTIVE", "ALREADY_CANCELLED", "该授权已经撤销。")
    return snapshot(row)


def verify_selected(db, item):
    current = current_snapshot(db, item["mandate_id"])
    require(current == snapshot(item), "VERSION_CONFLICT", "查询后授权内容已变化，需重新查询。")
    return current


def match_from_query(db, args, now):
    criterion = normalize(args["merchant_text"])
    require(bool(criterion), "INVALID_ARGUMENTS", "商户条件不能只有空白。")
    receipt = query_receipt(db, args["query_operation_id"], now)
    mode = args["match_mode"]
    candidates = []
    for item in receipt["mandates"]:
        name = normalize(item["name"])
        matches = name == criterion if mode == "normalized_equals" else criterion in name
        if matches:
            candidates.append(item)
    require(bool(candidates), "MANDATE_NO_MATCH", "查询回执中没有符合条件的授权。")
    require(len(candidates) == 1, "MANDATE_AMBIGUOUS", "查询回执中多个授权符合条件，请澄清。")
    selected = verify_selected(db, candidates[0])
    return {"status":"UNIQUE", "query_operation_id":args["query_operation_id"],
            "query_receipt_id":receipt["receipt_id"], "queried_at":receipt["committed_at"],
            "matched_at":now.isoformat(),
            "criterion":{"merchant_text":args["merchant_text"], "mode":mode},
            "selected":{k:selected[k] for k in ("mandate_id", "name", "version", "status")}}


def cancel_snapshot(db, params, now):
    """Called within prepare/confirm write transactions; no separate connection."""
    current = current_snapshot(db, params["mandate_id"])
    if "expected_version" in params:
        require(current["version"] == params["expected_version"],
                "VERSION_CONFLICT", "授权版本已变化，需重新查询。")
    if "query_operation_id" in params:
        require("expected_version" in params, "INVALID_ARGUMENTS", "查询来源必须同时绑定授权版本。")
        receipt = query_receipt(db, params["query_operation_id"], now)
        selected = next((item for item in receipt["mandates"]
                         if item["mandate_id"] == params["mandate_id"]), None)
        require(selected is not None, "INVALID_QUERY_RECEIPT", "授权不属于指定查询回执。")
        verify_selected(db, selected)
    return current
