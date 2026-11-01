"""Task behavior tests use disposable real bank databases, never user state."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone, timedelta
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from bank_v1 import Service
from bank_tasks import TaskService, TaskError


def node(identifier, tool="accounts", args=None, parents=None):
    return {"id": identifier, "tool": tool, "args": args or {}, "depends_on": parents or []}


class TaskTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = datetime(2026, 10, 12, 8, tzinfo=timezone.utc)
        self.bank = Service(self.root / "bank.db", clock=lambda: self.now)
        self.calls = []
        self.callback = self.bank.call_tool
        def call(name, args):
            self.calls.append((name, args))
            return self.callback(name, args)
        self.tasks = TaskService(self.root / "tasks.db", clock=lambda: self.now,
                                 tool_specs=self.bank.tool_specs, call_tool=call)

    def submit(self, nodes=None, key="request"):
        return self.tasks.submit("分析并在明确确认后办理", {"title": "办理", "nodes": nodes or [node("a")]}, key)

    def transfer(self, amount=50000):
        return node("pay", "prepare_transfer", {"source_account": "A", "target_account": "B", "amount_minor": amount})

    def confirm(self, task):
        op = task["nodes"][0]["result"]["data"]["operation"]
        self.bank.user_action("confirm", {"operation_id": op["operation_id"], "expected_summary": op["summary"]})

    def test_submit_idempotent_and_conflict(self):
        a = self.submit()
        self.assertEqual(a["task_id"], self.submit()["task_id"])
        with self.assertRaises(TaskError):
            self.submit([node("different")])

    def test_dependency_reference_and_confirmation(self):
        n = node("receipt", "operation", {"operation_id": {"$ref": "pay.data.operation.operation_id"}}, ["pay"])
        t = self.submit([self.transfer(), n])
        t = self.tasks.tick(t["task_id"])
        self.assertEqual(t["state"], "WAITING_CONFIRMATION")
        self.assertEqual(t["nodes"][1]["state"], "PENDING")
        before = len(self.bank.state()["transactions"])
        self.tasks.tick(t["task_id"])
        self.assertEqual(len(self.bank.state()["transactions"]), before)
        self.confirm(t)
        t = self.tasks.tick(t["task_id"])
        self.assertEqual(t["nodes"][0]["state"], "SUCCEEDED")
        t = self.tasks.tick(t["task_id"])
        self.assertEqual(t["state"], "SUCCEEDED")
        after = self.bank.state()["transactions"]
        self.tasks.tick(t["task_id"])
        self.assertEqual(after, self.bank.state()["transactions"])

    def test_strong_verification_wait(self):
        t = self.tasks.tick(self.submit([self.transfer(100001)])["task_id"])
        self.assertEqual(t["state"], "WAITING_VERIFICATION")
        self.assertEqual(self.bank.state()["operations"][0]["status"], "PENDING")

    def test_serialized_reference_rejected_before_any_bank_action(self):
        for index, value in enumerate(['{$ref:"pay.data.operation.operation_id"}',
                '{"$ref":"pay.data.operation.operation_id"}',
                "  { '$ref' : 'pay.data.operation.operation_id' }"]):
            with self.subTest(value=value), self.assertRaises(TaskError) as error:
                self.submit([self.transfer(), node("receipt", "operation",
                    {"operation_id": value}, ["pay"])], f"invalid-ref-{index}")
            self.assertEqual(error.exception.code, "INVALID_REFERENCE")
        self.assertEqual(self.calls, [])
        self.assertEqual(self.bank.state()["operations"], [])
        self.assertEqual(self.tasks.list(), [])

    def test_serialized_reference_replan_is_atomic(self):
        task = self.submit([node("a"), self.transfer()])
        task = self.tasks.tick(task["task_id"])
        invalid = [node("a"), self.transfer(), node("receipt", "operation",
            {"operation_id": '{"$ref":"pay.data.operation.operation_id"}'}, ["pay"])]
        with self.assertRaises(TaskError) as error:
            self.tasks.replan(task["task_id"], 1, invalid, "新增回执")
        self.assertEqual(error.exception.code, "INVALID_REFERENCE")
        self.assertEqual(self.tasks.get(task["task_id"]), task)
        self.assertEqual(self.bank.state()["operations"], [])

    def test_invalid_plans(self):
        samples = [[], [node("a", parents=["a"])], [node("a", parents=["missing"])],
                   [node("a"), node("a")], [node("a", "confirm")], [node("a", "submit_task_plan")],
                   [node("a", "accounts", {"subject_id": "U2"})],
                   [node("a", "forecast", {"through_date": "wrong"})],
                   [node("a", "operation", {"operation_id": {"$ref": "other.data.id"}}), node("other")],
                   [node("a", "forecast", {"through_date": {"$ref": "b.data.x"}, "proposed_expense_minor": -3}, ["b"]), node("b")]]
        for index, nodes in enumerate(samples):
            with self.subTest(index=index), self.assertRaises(TaskError):
                self.submit(nodes, str(index)) if nodes else self.tasks.submit("x", {"title": "x", "nodes": []}, str(index))

    def test_pause_resume_handoff(self):
        tid = self.submit()["task_id"]
        self.tasks.pause(tid)
        self.assertEqual(self.tasks.tick(tid)["state"], "PAUSED")
        self.assertEqual(self.calls, [])
        self.tasks.handoff(tid, "需要人工核对")
        self.assertEqual(self.tasks.tick(tid)["state"], "NEEDS_HUMAN")
        self.tasks.resume(tid)
        self.assertEqual(self.tasks.tick(tid)["state"], "SUCCEEDED")

    def test_reject_stops_dependents_and_cancel(self):
        t = self.tasks.tick(self.submit([self.transfer(), node("after", parents=["pay"])])["task_id"])
        with self.assertRaises(TaskError):
            self.tasks.cancel(t["task_id"])
        self.bank.user_action("reject", {"operation_id": t["nodes"][0]["operation_id"]})
        t = self.tasks.tick(t["task_id"])
        self.assertEqual(t["state"], "FAILED")
        self.assertEqual(t["nodes"][1]["state"], "PENDING")
        self.assertEqual(self.tasks.cancel(t["task_id"])["state"], "CANCELLED")

    def test_replan_preserves_success_and_changes_future(self):
        t = self.submit([node("a"), node("b", parents=["a"])])
        self.tasks.tick(t["task_id"])
        revised = [node("a"), node("later", "arrangements", parents=["a"])]
        t = self.tasks.replan(t["task_id"], 1, revised, "根据账户结果改查安排")
        self.assertEqual(t["plan_version"], 2)
        self.assertEqual(t["nodes"][0]["state"], "SUCCEEDED")
        self.assertEqual(len(t["plan_history"]), 1)
        with self.assertRaises(TaskError):
            self.tasks.replan(t["task_id"], 1, revised, "旧版本")
        with self.assertRaises(TaskError):
            self.tasks.replan(t["task_id"], 2, [node("later")], "抹掉成功")
        self.tasks.tick(t["task_id"])
        self.assertEqual([name for name, _ in self.calls], ["accounts", "arrangements"])

    def test_pending_replan_requires_rejection(self):
        t = self.tasks.tick(self.submit([self.transfer()])["task_id"])
        with self.assertRaises(TaskError):
            self.tasks.replan(t["task_id"], 1, [self.transfer(40000)], "换金额")
        op = t["nodes"][0]["operation_id"]
        self.bank.user_action("reject", {"operation_id": op})
        self.tasks.tick(t["task_id"])
        self.tasks.replan(t["task_id"], 1, [self.transfer(40000)], "用户改金额")
        t = self.tasks.tick(t["task_id"])
        self.assertNotEqual(op, t["nodes"][0]["operation_id"])
        self.assertEqual(t["state"], "WAITING_CONFIRMATION")

    def test_restart_preserves_success(self):
        tid = self.submit([node("a"), node("b", parents=["a"])])["task_id"]
        self.tasks.tick(tid)
        restarted = TaskService(self.root / "tasks.db", clock=lambda: self.now,
                                tool_specs=self.bank.tool_specs(), call_tool=self.bank.call_tool)
        result = restarted.tick(tid)
        self.assertEqual(result["state"], "SUCCEEDED")
        self.assertEqual([n["attempt"] for n in result["nodes"]], [1, 1])

    def test_concurrent_tick_single_execution(self):
        tid = self.submit([self.transfer()])["task_id"]
        with ThreadPoolExecutor(max_workers=4) as workers:
            list(workers.map(self.tasks.tick, [tid] * 8))
        self.assertEqual(len(self.bank.state()["operations"]), 1)
        self.assertEqual(sum(name == "prepare_transfer" for name, _ in self.calls), 1)

    def test_crash_after_bank_commit_recovers_same_key(self):
        class Crash(BaseException):
            pass
        def crash(name, args):
            self.bank.call_tool(name, args)
            raise Crash()
        self.callback = crash
        tid = self.submit([self.transfer()])["task_id"]
        with self.assertRaises(Crash):
            self.tasks.tick(tid)
        self.assertEqual(self.tasks.get(tid)["nodes"][0]["state"], "PENDING")
        with self.assertRaises(TaskError):
            self.tasks.replan(tid, 1, [self.transfer(40000)], "崩溃后换金额")
        with self.assertRaises(TaskError):
            self.tasks.cancel(tid)
        self.callback = self.bank.call_tool
        self.tasks.tick(tid)
        self.assertEqual(len(self.bank.state()["operations"]), 1)
        self.assertEqual(self.calls[0][1]["idempotency_key"], self.calls[1][1]["idempotency_key"])

    def test_timeout_unknown_prepare_requires_reconcile(self):
        def timeout(name, args):
            self.bank.call_tool(name, args)
            raise TimeoutError("secret should not appear")
        self.callback = timeout
        tid = self.submit([self.transfer()])["task_id"]
        t = self.tasks.tick(tid)
        self.assertNotIn("secret", str(t))
        with self.assertRaises(TaskError):
            self.tasks.replan(tid, 1, [self.transfer(40000)], "换金额")
        with self.assertRaises(TaskError):
            self.tasks.cancel(tid)
        self.callback = self.bank.call_tool
        self.tasks.retry(tid, "pay")
        self.assertEqual(self.tasks.tick(tid)["state"], "WAITING_CONFIRMATION")
        self.assertEqual(len(self.bank.state()["operations"]), 1)

    def test_bad_reference_fails_without_call(self):
        tid = self.submit([node("a"), node("b", "operation", {"operation_id": {"$ref": "a.data.missing"}}, ["a"])])["task_id"]
        self.tasks.tick(tid)
        t = self.tasks.tick(tid)
        self.assertEqual(t["nodes"][1]["error"]["code"], "MISSING_REFERENCE")
        self.assertEqual(len(self.calls), 1)

    def test_expired_requires_new_plan(self):
        t = self.tasks.tick(self.submit([self.transfer()])["task_id"])
        old_op = t["nodes"][0]["operation_id"]
        self.now += timedelta(minutes=6)
        t = self.tasks.tick(t["task_id"])
        self.assertEqual(t["nodes"][0]["error"]["code"], "EXPIRED")
        with self.assertRaises(TaskError):
            self.tasks.retry(t["task_id"], "pay")
        self.tasks.replan(t["task_id"], 1, [self.transfer()], "旧确认过期")
        t = self.tasks.tick(t["task_id"])
        self.assertNotEqual(old_op, t["nodes"][0]["operation_id"])

    def test_success_without_receipt_rejected(self):
        self.callback = lambda name, args: {"data": {"operation": {"operation_id": "fake", "status": "SUCCEEDED"}}}
        t = self.tasks.tick(self.submit([self.transfer()])["task_id"])
        self.assertEqual(t["state"], "FAILED")
        self.assertEqual(t["nodes"][0]["error"]["code"], "INVALID_RESULT")

    def test_repeat_failure_bounded(self):
        self.callback = lambda name, args: (_ for _ in ()).throw(TimeoutError())
        tid = self.submit()["task_id"]
        for _ in range(2):
            self.tasks.tick(tid)
            self.tasks.retry(tid, "a")
        self.tasks.tick(tid)
        with self.assertRaises(TaskError):
            self.tasks.retry(tid, "a")

    def test_definite_bank_precondition_failure_can_replan(self):
        available = next(a["balance_minor"] for a in self.bank.state()["accounts"] if a["account_id"] == "A")
        tid = self.submit([self.transfer(available + 1)])["task_id"]
        t = self.tasks.tick(tid)
        self.assertEqual(t["nodes"][0]["error"]["code"], "INSUFFICIENT_FUNDS")
        self.assertEqual([], self.bank.state()["operations"])
        self.tasks.replan(tid, 1, [self.transfer(40000)], "根据银行余额拒绝改金额")
        self.assertEqual(self.tasks.tick(tid)["state"], "WAITING_CONFIRMATION")

    def test_definite_bank_precondition_failure_can_cancel(self):
        tid = self.submit([self.transfer(100000000)])["task_id"]
        self.tasks.tick(tid)
        self.assertEqual(self.tasks.cancel(tid)["state"], "CANCELLED")

    def test_forged_error_code_does_not_release_unknown_intent(self):
        class UntrustedError(Exception):
            code = "INSUFFICIENT_FUNDS"
        def uncertain(name, args):
            self.bank.call_tool(name, args)
            raise UntrustedError()
        self.callback = uncertain
        tid = self.submit([self.transfer()])["task_id"]
        self.tasks.tick(tid)
        with self.assertRaises(TaskError):
            self.tasks.replan(tid, 1, [self.transfer(40000)], "同码未知结果")
        self.assertEqual(1, len(self.bank.state()["operations"]))


if __name__ == "__main__":
    unittest.main()
