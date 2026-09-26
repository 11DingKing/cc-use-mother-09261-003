"""SQLite 状态仓储：只追加（append-only）决定日志 + 乐观并发控制。

设计要点：

* ``decisions`` 是不可变事实表，每条记录是一次角色动作（含 ``create``）。
  触发器在数据库层面拒绝任何 ``UPDATE`` / ``DELETE``——历次决定无法被覆盖。
* 案件当前状态由决定日志回放得到：``version = COUNT(*)``。
* 每次动作都在 ``BEGIN IMMEDIATE`` 事务内进行：先取行锁，再校验
  ``expected_version``，随后追加决定。并发提交时只有一个版本能成功，
  其余收到 409 ``version_conflict``，调用方重试即可——最终状态一致。
* ``idempotency_keys`` 保证同键重试不产生重复决定。
"""
from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone

from .workflow import (
    CREATE_ACTION,
    CREATE_ROLE,
    InvalidTransition,
    PermissionDenied,
    WorkflowError,
    resolve,
)

INITIAL_STATE = "draft"


class VersionConflict(WorkflowError):
    """乐观版本冲突：案件已被其他并发决定推进。"""


class DuplicateCase(WorkflowError):
    """案件 ID 已存在。"""


SCHEMA = """
CREATE TABLE IF NOT EXISTS decisions (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    case_id      TEXT NOT NULL,
    version      INTEGER NOT NULL,
    actor        TEXT NOT NULL,
    role         TEXT NOT NULL,
    action       TEXT NOT NULL,
    from_state   TEXT,
    to_state     TEXT NOT NULL,
    payload      TEXT NOT NULL DEFAULT '{}',
    created_at   TEXT NOT NULL,
    UNIQUE(case_id, version)
);
CREATE INDEX IF NOT EXISTS idx_decisions_case ON decisions(case_id, seq);

CREATE TABLE IF NOT EXISTS idempotency_keys (
    key          TEXT PRIMARY KEY,
    case_id      TEXT NOT NULL,
    version      INTEGER NOT NULL
);

-- 历次决定不可覆盖：数据库层面禁止改写/删除事实
CREATE TRIGGER IF NOT EXISTS trg_decisions_no_update
BEFORE UPDATE ON decisions
BEGIN
    SELECT RAISE(ABORT, 'decisions are append-only: UPDATE forbidden');
END;
CREATE TRIGGER IF NOT EXISTS trg_decisions_no_delete
BEFORE DELETE ON decisions
BEGIN
    SELECT RAISE(ABORT, 'decisions are append-only: DELETE forbidden');
END;
"""


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


class SQLiteStore:
    """线程安全的 SQLite 仓储；每个连接绑定到创建它的线程。"""

    def __init__(self, path=":memory:"):
        self.path = path
        # check_same_thread=False + 自管锁：单写者串行化，配合 BEGIN IMMEDIATE
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = threading.RLock()
        self._conn.executescript(SCHEMA)

    def close(self):
        with self._lock:
            self._conn.close()

    # ------------------------------------------------------------------ #
    # 写路径
    # ------------------------------------------------------------------ #
    def create_case(self, case_id, actor, role=CREATE_ROLE, idempotency_key=None, payload=None):
        """提交者创建案件（version=1 的 create 决定）。"""
        if role != CREATE_ROLE:
            raise PermissionDenied(f"role '{role}' cannot create cases")
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if idempotency_key is not None:
                    hit = self._conn.execute(
                        "SELECT case_id, version FROM idempotency_keys WHERE key=?",
                        (idempotency_key,),
                    ).fetchone()
                    if hit is not None:
                        result = self._get_case(hit["case_id"])
                        self._conn.execute("COMMIT")
                        return result, True
                exists = self._conn.execute(
                    "SELECT 1 FROM decisions WHERE case_id=? LIMIT 1", (case_id,)
                ).fetchone()
                if exists is not None:
                    raise DuplicateCase(f"case already exists: {case_id}")
                ts = _now()
                self._conn.execute(
                    "INSERT INTO decisions"
                    "(case_id, version, actor, role, action, from_state, to_state, payload, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (case_id, 1, actor, role, CREATE_ACTION, None, INITIAL_STATE,
                     json.dumps(payload or {}, ensure_ascii=False), ts),
                )
                if idempotency_key is not None:
                    self._conn.execute(
                        "INSERT INTO idempotency_keys(key, case_id, version) VALUES (?,?,?)",
                        (idempotency_key, case_id, 1),
                    )
                self._conn.execute("COMMIT")
                return self._get_case(case_id), False
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def act(self, case_id, action, actor, role, expected_version=None,
            idempotency_key=None, payload=None):
        """对案件追加一个角色动作决定。

        返回 ``(案件视图, 是否幂等重放)``；状态/角色/版本不合法时抛
        :class:`WorkflowError` 子类。
        """
        # 先在锁外做纯函数校验，非法请求不占写事务
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                if idempotency_key is not None:
                    hit = self._conn.execute(
                        "SELECT case_id, version FROM idempotency_keys WHERE key=?",
                        (idempotency_key,),
                    ).fetchone()
                    if hit is not None:
                        result = self._get_case(hit["case_id"])
                        self._conn.execute("COMMIT")
                        return result, True

                current = self._current_state(case_id)
                if current is None:
                    raise InvalidTransition(f"case not found: {case_id}")
                state, version = current
                if expected_version is not None and expected_version != version:
                    raise VersionConflict(
                        f"expected version {expected_version} but case is at {version}"
                    )
                to_state = resolve(action, state, role)
                ts = _now()
                self._conn.execute(
                    "INSERT INTO decisions"
                    "(case_id, version, actor, role, action, from_state, to_state, payload, created_at)"
                    " VALUES (?,?,?,?,?,?,?,?,?)",
                    (case_id, version + 1, actor, role, action, state, to_state,
                     json.dumps(payload or {}, ensure_ascii=False), ts),
                )
                if idempotency_key is not None:
                    self._conn.execute(
                        "INSERT INTO idempotency_keys(key, case_id, version) VALUES (?,?,?)",
                        (idempotency_key, case_id, version + 1),
                    )
                self._conn.execute("COMMIT")
                return self._get_case(case_id), False
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    # ------------------------------------------------------------------ #
    # 读路径
    # ------------------------------------------------------------------ #
    def _current_state(self, case_id):
        row = self._conn.execute(
            "SELECT to_state, version FROM decisions WHERE case_id=? ORDER BY seq DESC LIMIT 1",
            (case_id,),
        ).fetchone()
        if row is None:
            return None
        return row["to_state"], row["version"]

    def _get_case(self, case_id):
        rows = self._conn.execute(
            "SELECT version, actor, role, action, from_state, to_state, payload, created_at"
            " FROM decisions WHERE case_id=? ORDER BY seq",
            (case_id,),
        ).fetchall()
        if not rows:
            return None
        first, last = rows[0], rows[-1]
        return {
            "id": case_id,
            "state": last["to_state"],
            "version": last["version"],
            "created_by": first["actor"],
            "created_at": first["created_at"],
            "updated_at": last["created_at"],
            "history": [
                {
                    "version": r["version"],
                    "actor": r["actor"],
                    "role": r["role"],
                    "action": r["action"],
                    "from_state": r["from_state"],
                    "to_state": r["to_state"],
                    "payload": json.loads(r["payload"]),
                    "created_at": r["created_at"],
                }
                for r in rows
            ],
        }

    def get_case(self, case_id):
        with self._lock:
            return self._get_case(case_id)

    def list_cases(self):
        with self._lock:
            ids = [
                r["case_id"]
                for r in self._conn.execute(
                    "SELECT case_id, MAX(seq) AS m FROM decisions GROUP BY case_id ORDER BY m"
                )
            ]
            return [self._get_case(cid) for cid in ids]

    def latest(self):
        """所有案件的精简快照（决定日志回放的投影）。"""
        with self._lock:
            return [
                {"id": c["id"], "actor": c["created_by"],
                 "state": c["state"], "version": c["version"]}
                for c in self.list_cases()
            ]
