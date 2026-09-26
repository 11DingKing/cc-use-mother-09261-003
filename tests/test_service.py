"""服务端测试：领域规则、持久化、追加式日志、HTTP API 与并发最终状态。"""
import http.client
import json
import os
import sqlite3
import tempfile
import threading
import unittest

from service_09261_003.api import make_server
from service_09261_003.service import CaseService
from service_09261_003.store import SQLiteStore
from service_09261_003.workflow import Conflict, Forbidden, ValidationError


class ServiceTestCase(unittest.TestCase):
    """每个用例一个独立的文件库，验证真实持久化路径。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "test.db")
        self.service = CaseService(SQLiteStore(self.db_path))

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, cid="c1", key=None):
        return self.service.create_case(
            case_id=cid, actor="alice", role="submitter", idempotency_key=key)

    def submit(self, cid="c1", key=None, **kw):
        return self.service.apply_action(
            case_id=cid, action="submit", actor="alice", role="submitter",
            idempotency_key=key, **kw)


class TestWorkflow(ServiceTestCase):
    def test_happy_path_full_pipeline(self):
        case, decision, created = self.create()
        self.assertTrue(created)
        self.assertEqual((case.state, case.version), ("draft", 1))
        self.assertEqual(decision.action, "create")

        case, _, _ = self.submit()
        self.assertEqual((case.state, case.version), ("reviewing", 2))

        case, _, _ = self.service.apply_action(
            case_id="c1", action="approve", actor="bob", role="reviewer")
        self.assertEqual((case.state, case.version), ("approved", 3))

        case, _, _ = self.service.apply_action(
            case_id="c1", action="publish", actor="carol", role="publisher")
        self.assertEqual((case.state, case.version), ("published", 4))

        history = self.service.list_decisions("c1")
        self.assertEqual([d.action for d in history],
                         ["create", "submit", "approve", "publish"])
        self.assertEqual([d.version for d in history], [1, 2, 3, 4])

    def test_reject_then_revise_then_resubmit(self):
        self.create()
        self.submit()
        self.service.apply_action(case_id="c1", action="reject",
                                  actor="bob", role="reviewer")
        case, _, _ = self.service.apply_action(
            case_id="c1", action="revise", actor="alice", role="submitter")
        self.assertEqual(case.state, "draft")
        case, _, _ = self.submit()
        self.assertEqual(case.state, "reviewing")

    def test_role_enforcement(self):
        self.create()
        with self.assertRaises(Forbidden):
            self.service.apply_action(case_id="c1", action="submit",
                                      actor="bob", role="reviewer")
        self.submit()
        with self.assertRaises(Forbidden):
            self.service.apply_action(case_id="c1", action="approve",
                                      actor="alice", role="submitter")
        with self.assertRaises(Forbidden):
            self.service.apply_action(case_id="c1", action="approve",
                                      actor="carol", role="publisher")
        with self.assertRaises(ValidationError):
            self.service.apply_action(case_id="c1", action="approve",
                                      actor="x", role="boss")

    def test_invalid_transition_and_unknown_action(self):
        self.create()
        with self.assertRaises(Conflict):
            self.service.apply_action(case_id="c1", action="approve",
                                      actor="bob", role="reviewer")
        with self.assertRaises(ValidationError):
            self.service.apply_action(case_id="c1", action="delete",
                                      actor="a", role="submitter")

    def test_expected_version_conflict(self):
        self.create()
        with self.assertRaises(Conflict):
            self.submit(expected_version=9)
        case, _, applied = self.submit(expected_version=1)
        self.assertTrue(applied)
        self.assertEqual(case.version, 2)

    def test_idempotent_replay(self):
        # 同一幂等键重复创建：只产生一条决定
        self.create(key="k-create")
        case, decision, created = self.create(key="k-create")
        self.assertFalse(created)
        self.assertEqual(case.version, 1)
        self.assertEqual(len(self.service.list_decisions("c1")), 1)

        # 同一幂等键重复提交：状态不前进
        self.submit(key="k-submit")
        case, decision, applied = self.submit(key="k-submit")
        self.assertFalse(applied)
        self.assertEqual(case.version, 2)
        self.assertEqual(decision.action, "submit")
        self.assertEqual(len(self.service.list_decisions("c1")), 2)

    def test_duplicate_case_conflict(self):
        self.create()
        with self.assertRaises(Conflict):
            self.create()


class TestStore(ServiceTestCase):
    def test_persistence_across_reopen(self):
        self.create()
        self.submit()
        reopened = CaseService(SQLiteStore(self.db_path))
        case = reopened.get_case("c1")
        self.assertEqual((case.state, case.version), ("reviewing", 2))
        self.assertEqual(len(reopened.list_decisions("c1")), 2)

    def test_decisions_are_append_only(self):
        self.create()
        store = self.service.store
        with store.tx() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("UPDATE decisions SET reason='tampered'")
        with store.tx() as conn:
            with self.assertRaises(sqlite3.IntegrityError):
                conn.execute("DELETE FROM decisions")
        # 原记录完好
        history = self.service.list_decisions("c1")
        self.assertEqual(len(history), 1)
        self.assertEqual(history[0].action, "create")


class HttpTestCase(unittest.TestCase):
    """启动真实 HTTP 服务（临时端口），通过 HTTP 客户端验证。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db_path = os.path.join(self.tmp.name, "http.db")
        self.service = CaseService(SQLiteStore(db_path))
        self.server = make_server("127.0.0.1", 0, self.service, quiet=True)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.tmp.cleanup()

    def req(self, method, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        payload = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, path, body=payload, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        conn.close()
        return resp.status, json.loads(raw.decode("utf-8")) if raw else {}

    def post_case(self, cid, key=None):
        body = {"id": cid, "actor": "alice", "role": "submitter"}
        if key:
            body["idempotency_key"] = key
        return self.req("POST", "/cases", body)

    def post_action(self, cid, action, role, actor="bob", key=None):
        body = {"action": action, "actor": actor, "role": role}
        if key:
            body["idempotency_key"] = key
        return self.req("POST", f"/cases/{cid}/decisions", body)

    def assert_history_consistent(self, cid):
        """最终状态不变量：历史链完整、版本连续、末态等于当前态。"""
        _, case_body = self.req("GET", f"/cases/{cid}")
        _, hist_body = self.req("GET", f"/cases/{cid}/decisions")
        case, history = case_body["case"], hist_body["decisions"]
        self.assertEqual(case["version"], len(history))
        self.assertEqual([d["version"] for d in history],
                         list(range(1, len(history) + 1)))
        for prev, cur in zip(history, history[1:]):
            self.assertEqual(cur["from_state"], prev["to_state"])
        self.assertEqual(history[-1]["to_state"], case["state"])
        return case, history


class TestHttpApi(HttpTestCase):
    def test_end_to_end_over_http(self):
        status, body = self.post_case("c1", key="k1")
        self.assertEqual(status, 201)
        self.assertEqual(body["case"]["state"], "draft")

        # 重复同一幂等键 → 200 重放，不产生新决定
        status, body = self.post_case("c1", key="k1")
        self.assertEqual(status, 200)
        self.assertTrue(body["idempotent_replay"])

        status, _ = self.post_action("c1", "submit", "submitter", actor="alice")
        self.assertEqual(status, 201)
        status, body = self.post_action("c1", "approve", "reviewer")
        self.assertEqual(status, 201)
        status, body = self.post_action("c1", "publish", "publisher",
                                        actor="carol")
        self.assertEqual(status, 201)
        self.assertEqual(body["case"]["state"], "published")

        case, history = self.assert_history_consistent("c1")
        self.assertEqual(case["version"], 4)
        self.assertEqual([d["actor"] for d in history],
                         ["alice", "alice", "bob", "carol"])

    def test_error_mapping_over_http(self):
        self.post_case("c1")
        # 越权：复核者不能提交
        status, body = self.post_action("c1", "submit", "reviewer")
        self.assertEqual((status, body["error"]), (403, "forbidden"))
        # 非法流转：draft 不能 approve
        status, body = self.post_action("c1", "approve", "reviewer")
        self.assertEqual((status, body["error"]), (409, "conflict"))
        # 缺字段
        status, body = self.req("POST", "/cases", {"id": "c2"})
        self.assertEqual((status, body["error"]), (400, "validation"))
        # 不存在
        status, body = self.req("GET", "/cases/nope")
        self.assertEqual((status, body["error"]), (404, "not_found"))
        # 健康检查
        status, body = self.req("GET", "/healthz")
        self.assertEqual((status, body["status"]), (200, "ok"))


class TestConcurrency(HttpTestCase):
    """并发提交的最终状态验证：多线程同时打 HTTP 接口。"""

    THREADS = 8

    def run_parallel(self, fn, args_list):
        results, errors = [], []

        def worker(arg):
            try:
                results.append(fn(arg))
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(a,))
                   for a in args_list]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        self.assertEqual(errors, [])
        return results

    def test_concurrent_create_same_idempotency_key(self):
        """N 个并发相同幂等键创建 → 恰好一个案件、一条决定。"""
        results = self.run_parallel(
            lambda _: self.post_case("c1", key="shared-key"),
            range(self.THREADS))
        statuses = sorted(s for s, _ in results)
        self.assertEqual(statuses.count(201), 1)
        self.assertEqual(statuses.count(200), self.THREADS - 1)
        _, body = self.req("GET", "/cases")
        self.assertEqual(len(body["cases"]), 1)
        case, history = self.assert_history_consistent("c1")
        self.assertEqual((case["version"], len(history)), (1, 1))

    def test_concurrent_create_same_id_distinct_keys(self):
        """N 个并发不同键创建同一 id → 恰好一个成功，其余 409。"""
        results = self.run_parallel(
            lambda i: self.post_case("c1", key=f"key-{i}"),
            range(self.THREADS))
        statuses = [s for s, _ in results]
        self.assertEqual(statuses.count(201), 1)
        self.assertEqual(statuses.count(409), self.THREADS - 1)
        _, body = self.req("GET", "/cases")
        self.assertEqual(len(body["cases"]), 1)

    def test_concurrent_review_single_winner(self):
        """复核动作并发竞争 → 恰好一个生效，最终状态与历史一致。"""
        self.post_case("c1")
        self.post_action("c1", "submit", "submitter", actor="alice")

        actions = ["approve", "reject"] * (self.THREADS // 2)
        results = self.run_parallel(
            lambda action: self.post_action("c1", action, "reviewer"),
            actions)
        winners = [(s, b) for (s, b), a in zip(results, actions) if s == 201]
        losers = [(s, b) for (s, b), a in zip(results, actions) if s == 409]
        self.assertEqual(len(winners), 1)
        self.assertEqual(len(losers), self.THREADS - 1)

        case, history = self.assert_history_consistent("c1")
        self.assertEqual(case["version"], 3)  # create + submit + 唯一生效的复核
        self.assertIn(case["state"], ("approved", "rejected"))
        self.assertEqual(history[-1]["action"],
                         winners[0][1]["decision"]["action"])
        self.assertEqual(history[-1]["to_state"], case["state"])

    def test_concurrent_distinct_cases_final_state(self):
        """N 个线程各自创建并提交不同案件 → 最终快照完整无丢失。"""
        n = 16
        self.run_parallel(lambda i: self.post_case(f"case-{i}"), range(n))
        self.run_parallel(
            lambda i: self.post_action(f"case-{i}", "submit", "submitter",
                                       actor="alice"),
            range(n))
        _, body = self.req("GET", "/cases")
        cases = {c["id"]: c for c in body["cases"]}
        self.assertEqual(len(cases), n)
        for i in range(n):
            self.assertEqual(
                (cases[f"case-{i}"]["state"], cases[f"case-{i}"]["version"]),
                ("reviewing", 2))
            self.assert_history_consistent(f"case-{i}")


if __name__ == "__main__":
    unittest.main()
