"""HTTP 端到端测试：真实 socket 调用，覆盖角色分权、409、幂等等边界。"""
import json
import threading
import unittest
import urllib.error
import urllib.request

from service_09261_003.api import create_server
from service_09261_003.store import SQLiteStore
from service_09261_003.workflow import PUBLISHER, REVIEWER, SUBMITTER


class HttpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = SQLiteStore(":memory:")
        cls.httpd = create_server("127.0.0.1", 0, cls.store)
        cls.base = f"http://127.0.0.1:{cls.httpd.server_address[1]}"
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.store.close()

    def req(self, method, path, body=None, headers=None):
        data = None
        hdrs = headers or {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        r = urllib.request.Request(self.base + path, data=data,
                                   method=method, headers=hdrs)
        try:
            with urllib.request.urlopen(r, timeout=5) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode("utf-8"))

    def test_health(self):
        status, body = self.req("GET", "/healthz")
        self.assertEqual(status, 200)
        self.assertEqual(body["status"], "ok")

    def test_full_workflow_over_http(self):
        status, body = self.req("POST", "/cases",
                                {"id": "case-http", "actor": "alice",
                                 "role": SUBMITTER})
        self.assertEqual(status, 201)
        self.assertEqual(body["case"]["state"], "draft")

        # 提交者提交复核
        status, body = self.req("POST", "/cases/case-http/actions",
                                {"action": "submit", "actor": "alice",
                                 "role": SUBMITTER, "expected_version": 1})
        self.assertEqual(status, 200)
        self.assertEqual(body["case"]["version"], 2)

        # 复核者批准
        status, body = self.req("POST", "/cases/case-http/actions",
                                {"action": "approve", "actor": "bob",
                                 "role": REVIEWER})
        self.assertEqual(status, 200)

        # 发布者发布
        status, body = self.req("POST", "/cases/case-http/actions",
                                {"action": "publish", "actor": "carol",
                                 "role": PUBLISHER})
        self.assertEqual(status, 200)

        status, body = self.req("GET", "/cases/case-http")
        self.assertEqual(status, 200)
        self.assertEqual(body["state"], "published")
        self.assertEqual(len(body["history"]), 4)

        status, body = self.req("GET", "/cases/case-http/history")
        self.assertEqual(status, 200)
        self.assertEqual([d["action"] for d in body["decisions"]],
                         ["create", "submit", "approve", "publish"])

        status, body = self.req("GET", "/cases")
        self.assertEqual(status, 200)
        self.assertIn("case-http", [c["id"] for c in body["cases"]])

    def test_cross_role_forbidden_403(self):
        self.req("POST", "/cases",
                 {"id": "case-403", "actor": "alice", "role": SUBMITTER})
        status, body = self.req("POST", "/cases/case-403/actions",
                                {"action": "approve", "actor": "alice",
                                 "role": SUBMITTER})
        self.assertEqual(status, 403)
        self.assertEqual(body["error"], "permission_denied")

    def test_invalid_transition_422(self):
        self.req("POST", "/cases",
                 {"id": "case-422", "actor": "alice", "role": SUBMITTER})
        status, body = self.req("POST", "/cases/case-422/actions",
                                {"action": "publish", "actor": "carol",
                                 "role": PUBLISHER})
        self.assertEqual(status, 422)

    def test_version_conflict_409(self):
        self.req("POST", "/cases",
                 {"id": "case-409", "actor": "alice", "role": SUBMITTER})
        # 推进到 v2
        self.req("POST", "/cases/case-409/actions",
                 {"action": "submit", "actor": "alice", "role": SUBMITTER})
        # 拿着过期的 v1 去操作 -> 409
        status, body = self.req("POST", "/cases/case-409/actions",
                                {"action": "approve", "actor": "bob",
                                 "role": REVIEWER, "expected_version": 1})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "version_conflict")

    def test_duplicate_case_409(self):
        self.req("POST", "/cases",
                 {"id": "case-dup", "actor": "alice", "role": SUBMITTER})
        status, body = self.req("POST", "/cases",
                                {"id": "case-dup", "actor": "alice",
                                 "role": SUBMITTER})
        self.assertEqual(status, 409)
        self.assertEqual(body["error"], "duplicate_case")

    def test_idempotent_replay_over_http(self):
        payload = {"id": "case-idem", "actor": "alice", "role": SUBMITTER,
                   "idempotency_key": "idem-1"}
        s1, b1 = self.req("POST", "/cases", payload)
        s2, b2 = self.req("POST", "/cases", payload)
        self.assertEqual(s1, 201)
        self.assertEqual(s2, 200)
        self.assertTrue(b2["idempotent_replay"])
        self.assertEqual(b1["case"]["version"], b2["case"]["version"])

    def test_bad_requests_and_404(self):
        status, _ = self.req("POST", "/cases", {"id": "x"})  # 缺角色
        self.assertEqual(status, 400)
        status, body = self.req("POST", "/cases",
                                {"id": "y", "actor": "a", "role": "hacker"})
        self.assertEqual(status, 400)
        status, body = self.req("GET", "/cases/missing")
        self.assertEqual(status, 404)
        status, body = self.req("POST", "/cases/missing/actions",
                                {"action": "submit", "actor": "a",
                                 "role": SUBMITTER})
        self.assertEqual(status, 404)
        status, _ = self.req("POST", "/cases/missing/actions",
                             {"action": "frobnicate", "actor": "a",
                              "role": SUBMITTER})
        self.assertEqual(status, 400)

    def test_role_via_headers(self):
        status, body = self.req(
            "POST", "/cases", {"id": "case-hdr"},
            headers={"X-Actor": "alice", "X-Role": SUBMITTER,
                     "Content-Type": "application/json"})
        self.assertEqual(status, 201)


if __name__ == "__main__":
    unittest.main()
