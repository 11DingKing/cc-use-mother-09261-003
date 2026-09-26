"""SQLite 仓储测试：只追加、幂等、持久化重开、并发最终状态。"""
import os
import sqlite3
import tempfile
import threading
import unittest

from service_09261_003.store import (
    DuplicateCase,
    SQLiteStore,
    VersionConflict,
)
from service_09261_003.workflow import (
    PUBLISHER,
    REVIEWER,
    SUBMITTER,
    InvalidTransition,
    PermissionDenied,
)


class TestStoreCore(unittest.TestCase):
    def setUp(self):
        self.store = SQLiteStore(":memory:")

    def tearDown(self):
        self.store.close()

    def test_lifecycle_and_versions(self):
        case, _ = self.store.create_case("c1", "alice")
        self.assertEqual(case["state"], "draft")
        self.assertEqual(case["version"], 1)
        case, _ = self.store.act("c1", "submit", "alice", SUBMITTER)
        self.assertEqual((case["state"], case["version"]), ("reviewing", 2))
        case, _ = self.store.act("c1", "approve", "bob", REVIEWER)
        self.assertEqual((case["state"], case["version"]), ("approved", 3))
        case, _ = self.store.act("c1", "publish", "carol", PUBLISHER)
        case, _ = self.store.act("c1", "archive", "carol", PUBLISHER)
        self.assertEqual((case["state"], case["version"]), ("archived", 5))
        # 完整决定流：历次动作全部保留
        self.assertEqual([d["action"] for d in case["history"]],
                         ["create", "submit", "approve", "publish", "archive"])

    def test_role_enforced_in_store(self):
        self.store.create_case("c1", "alice")
        with self.assertRaises(PermissionDenied):
            self.store.act("c1", "approve", "alice", SUBMITTER)
        self.store.act("c1", "submit", "alice", SUBMITTER)
        with self.assertRaises(PermissionDenied):
            self.store.act("c1", "publish", "bob", REVIEWER)

    def test_invalid_transition_and_missing_case(self):
        self.store.create_case("c1", "alice")
        with self.assertRaises(InvalidTransition):
            self.store.act("c1", "publish", "carol", PUBLISHER)
        with self.assertRaises(InvalidTransition):
            self.store.act("nope", "submit", "x", SUBMITTER)

    def test_duplicate_create(self):
        self.store.create_case("c1", "alice")
        with self.assertRaises(DuplicateCase):
            self.store.create_case("c1", "alice")

    def test_idempotency_replay(self):
        case1, first = self.store.create_case(
            "c1", "alice", idempotency_key="key-1")
        self.assertFalse(first)
        case2, replay = self.store.create_case(
            "c1", "alice", idempotency_key="key-1")
        self.assertTrue(replay)
        self.assertEqual(case1["version"], case2["version"])
        # 动作幂等：同键重复提交不产生新版本
        self.store.act("c1", "submit", "alice", SUBMITTER,
                       idempotency_key="key-2")
        case, replay = self.store.act("c1", "submit", "alice", SUBMITTER,
                                      idempotency_key="key-2")
        self.assertTrue(replay)
        self.assertEqual(case["version"], 2)

    def test_only_submitter_may_create(self):
        with self.assertRaises(PermissionDenied):
            self.store.create_case("c1", "bob", role=REVIEWER)


class TestAppendOnly(unittest.TestCase):
    def setUp(self):
        self.store = SQLiteStore(":memory:")
        self.store.create_case("c1", "alice")

    def tearDown(self):
        self.store.close()

    def test_update_is_blocked_by_trigger(self):
        with self.assertRaises(sqlite3.Error) as cm:
            self.store._conn.execute(
                "UPDATE decisions SET to_state='published' WHERE case_id='c1'")
        self.assertIn("append-only", str(cm.exception))

    def test_delete_is_blocked_by_trigger(self):
        with self.assertRaises(sqlite3.Error) as cm:
            self.store._conn.execute("DELETE FROM decisions")
        self.assertIn("append-only", str(cm.exception))

    def test_history_cannot_be_rewritten_after_failed_attempt(self):
        before = self.store.get_case("c1")["history"]
        with self.assertRaises(sqlite3.Error):
            self.store._conn.execute("DELETE FROM decisions")
        after = self.store.get_case("c1")["history"]
        self.assertEqual(before, after)


class TestPersistence(unittest.TestCase):
    def test_state_survives_reopen(self):
        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            store = SQLiteStore(path)
            store.create_case("c1", "alice")
            store.act("c1", "submit", "alice", SUBMITTER)
            store.act("c1", "reject", "bob", REVIEWER)
            store.close()

            store2 = SQLiteStore(path)  # 触发器是 IF NOT EXISTS，重建幂等
            case = store2.get_case("c1")
            self.assertEqual(case["state"], "rejected")
            self.assertEqual(case["version"], 3)
            self.assertEqual(len(case["history"]), 3)
            store2.close()
        finally:
            for suffix in ("", "-wal", "-shm"):
                p = path + suffix
                if os.path.exists(p):
                    os.remove(p)


class TestConcurrency(unittest.TestCase):
    def setUp(self):
        self.store = SQLiteStore(":memory:")
        self.store.create_case("c1", "alice")
        self.store.act("c1", "submit", "alice", SUBMITTER)  # v2 reviewing

    def tearDown(self):
        self.store.close()

    def test_concurrent_stale_versions_exactly_one_wins(self):
        """20 个复核者拿着同一个版本号并发 approve：恰好一个成功。"""
        n = 20
        results = []
        lock = threading.Lock()
        barrier = threading.Barrier(n)

        def attempt():
            barrier.wait()  # 尽量把所有线程挤压到同一刻发起
            try:
                self.store.act("c1", "approve", "bob", REVIEWER,
                               expected_version=2)
                with lock:
                    results.append("ok")
            except VersionConflict:
                with lock:
                    results.append("conflict")
            except Exception as exc:  # pragma: no cover
                with lock:
                    results.append(f"other:{exc!r}")

        threads = [threading.Thread(target=attempt) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(results.count("ok"), 1)
        self.assertEqual(results.count("conflict"), n - 1)
        case = self.store.get_case("c1")
        self.assertEqual(case["state"], "approved")
        self.assertEqual(case["version"], 3)
        # 没有重复版本号，没有丢失决定
        versions = [d["version"] for d in case["history"]]
        self.assertEqual(versions, [1, 2, 3])

    def test_concurrent_without_version_also_no_lost_update(self):
        """不带版本号的并发写入由写锁串行裁决：同样只有一条生效。"""
        n = 20
        outcomes = {"ok": 0, "rejected": 0}
        barrier = threading.Barrier(n)

        def attempt():
            barrier.wait()
            try:
                self.store.act("c1", "approve", "bob", REVIEWER)
                outcomes["ok"] += 1
            except InvalidTransition:
                outcomes["rejected"] += 1

        threads = [threading.Thread(target=attempt) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(outcomes["ok"], 1)
        self.assertEqual(outcomes["rejected"], n - 1)
        self.assertEqual(self.store.get_case("c1")["version"], 3)

    def test_conflicts_then_retry_reach_consistent_final_state(self):
        """失败者重读最新版本后重试：多角色流水线并发推进，最终状态唯一。"""
        # 新案件停在 approved(v3)，发布者们并发 publish，冲突方重试时
        # 发现已是 published 即停止——最终恰好 published v4。
        self.store.act("c1", "approve", "bob", REVIEWER)
        n = 16
        barrier = threading.Barrier(n)

        def publish():
            barrier.wait()
            for _ in range(10):
                case = self.store.get_case("c1")
                if case["state"] != "approved":
                    return
                try:
                    self.store.act("c1", "publish", "carol", PUBLISHER,
                                   expected_version=case["version"])
                    return
                except VersionConflict:
                    continue

        threads = [threading.Thread(target=publish) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        case = self.store.get_case("c1")
        self.assertEqual(case["state"], "published")
        self.assertEqual(case["version"], 4)

    def test_concurrent_distinct_cases_all_commit(self):
        n = 20
        barrier = threading.Barrier(n)

        def create(i):
            barrier.wait()
            self.store.create_case(f"cc{i}", "alice")

        threads = [threading.Thread(target=create, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(self.store.list_cases()), n + 1)


if __name__ == "__main__":
    unittest.main()
