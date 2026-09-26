"""SQLite 持久化：cases 当前状态 + decisions 追加式决定日志。

- decisions 表通过触发器禁止 UPDATE/DELETE，历次决定不可被覆盖；
- UNIQUE(case_id, version) 保证同一 case 的决定序列不会分叉；
- idempotency_key 唯一索引支撑幂等重放；
- 写操作一律走 BEGIN IMMEDIATE 事务，配合乐观版本检查保证并发安全。
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager

from .workflow import Case, Decision

SCHEMA = """
CREATE TABLE IF NOT EXISTS cases(
  id         TEXT PRIMARY KEY,
  state      TEXT NOT NULL,
  version    INTEGER NOT NULL,
  created_by TEXT NOT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS decisions(
  seq             INTEGER PRIMARY KEY AUTOINCREMENT,
  case_id         TEXT NOT NULL REFERENCES cases(id),
  version         INTEGER NOT NULL,
  action          TEXT NOT NULL,
  from_state      TEXT NOT NULL,
  to_state        TEXT NOT NULL,
  actor           TEXT NOT NULL,
  role            TEXT NOT NULL,
  reason          TEXT NOT NULL DEFAULT '',
  idempotency_key TEXT,
  decided_at      TEXT NOT NULL,
  UNIQUE(case_id, version)
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_decisions_key
  ON decisions(idempotency_key) WHERE idempotency_key IS NOT NULL;
CREATE TRIGGER IF NOT EXISTS decisions_append_only_update
BEFORE UPDATE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END;
CREATE TRIGGER IF NOT EXISTS decisions_append_only_delete
BEFORE DELETE ON decisions
BEGIN SELECT RAISE(ABORT, 'decisions are append-only'); END;
"""


class SQLiteStore:
    """线程安全的 SQLite 仓储。

    文件库：每次调用新建连接（WAL 模式下读写互不阻塞），写事务由
    进程内锁串行化；":memory:" 库：单连接 + 全操作串行化。
    """

    def __init__(self, path: str = ":memory:"):
        self.path = path
        self._lock = threading.RLock()
        self._keeper: sqlite3.Connection | None = None
        if path == ":memory:":
            self._keeper = self._connect()
            self._init_schema(self._keeper)
        else:
            conn = self._connect()
            try:
                self._init_schema(conn)
            finally:
                conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None,
                               check_same_thread=False)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    @staticmethod
    def _init_schema(conn: sqlite3.Connection) -> None:
        conn.executescript(SCHEMA)

    @contextmanager
    def tx(self):
        """写事务上下文：BEGIN IMMEDIATE，异常自动回滚。"""
        with self._lock:
            conn, owned = (self._keeper, False) if self._keeper is not None \
                else (self._connect(), True)
            try:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    yield conn
                    conn.execute("COMMIT")
                except BaseException:
                    conn.execute("ROLLBACK")
                    raise
            finally:
                if owned:
                    conn.close()

    def _read(self, fn):
        if self._keeper is not None:
            with self._lock:
                return fn(self._keeper)
        conn = self._connect()
        try:
            return fn(conn)
        finally:
            conn.close()

    # ---- 行映射 ----

    @staticmethod
    def _to_case(row: sqlite3.Row) -> Case:
        return Case(id=row["id"], state=row["state"], version=row["version"],
                    created_by=row["created_by"], created_at=row["created_at"],
                    updated_at=row["updated_at"])

    @staticmethod
    def _to_decision(row: sqlite3.Row) -> Decision:
        return Decision(case_id=row["case_id"], version=row["version"],
                        action=row["action"], from_state=row["from_state"],
                        to_state=row["to_state"], actor=row["actor"],
                        role=row["role"], reason=row["reason"],
                        idempotency_key=row["idempotency_key"],
                        decided_at=row["decided_at"], seq=row["seq"])

    # ---- 读 ----

    def get_case(self, conn, case_id: str) -> Case | None:
        row = conn.execute("SELECT * FROM cases WHERE id=?", (case_id,)).fetchone()
        return self._to_case(row) if row else None

    def case(self, case_id: str) -> Case | None:
        return self._read(lambda c: self.get_case(c, case_id))

    def list_cases(self) -> list[Case]:
        return self._read(lambda c: [self._to_case(r) for r in c.execute(
            "SELECT * FROM cases ORDER BY id")])

    def list_decisions(self, case_id: str) -> list[Decision]:
        return self._read(lambda c: [self._to_decision(r) for r in c.execute(
            "SELECT * FROM decisions WHERE case_id=? ORDER BY version",
            (case_id,))])

    def decision_by_key(self, conn, key: str) -> Decision | None:
        row = conn.execute(
            "SELECT * FROM decisions WHERE idempotency_key=?", (key,)).fetchone()
        return self._to_decision(row) if row else None

    # ---- 写（须在 tx() 内调用） ----

    def insert_case(self, conn, case: Case) -> None:
        conn.execute(
            "INSERT INTO cases(id,state,version,created_by,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?)",
            (case.id, case.state, case.version, case.created_by,
             case.created_at, case.updated_at))

    def insert_decision(self, conn, decision: Decision) -> Decision:
        cur = conn.execute(
            "INSERT INTO decisions(case_id,version,action,from_state,to_state,"
            "actor,role,reason,idempotency_key,decided_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?)",
            (decision.case_id, decision.version, decision.action,
             decision.from_state, decision.to_state, decision.actor,
             decision.role, decision.reason, decision.idempotency_key,
             decision.decided_at))
        return Decision(**{**decision.__dict__, "seq": cur.lastrowid})

    def advance_case(self, conn, case_id: str, expected_version: int,
                     state: str, updated_at: str) -> bool:
        """乐观推进当前状态：版本不匹配则拒绝，防止并发覆盖。"""
        cur = conn.execute(
            "UPDATE cases SET state=?, version=version+1, updated_at=?"
            " WHERE id=? AND version=?",
            (state, updated_at, case_id, expected_version))
        return cur.rowcount == 1
