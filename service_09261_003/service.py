"""应用服务层：输入校验 + 领域规则 + 原子持久化。

每个写操作在单个事务内完成「查重 → 校验 → 写决定 → 推进状态」，
因此并发请求要么完整生效、要么整体失败，不会留下半截状态。
"""
from __future__ import annotations

import sqlite3

from .store import SQLiteStore
from .workflow import (
    ACTIONS,
    Case,
    Conflict,
    Decision,
    NotFound,
    ValidationError,
    check_transition,
    require_role,
    utcnow,
)


def _need_str(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} is required and must be a non-empty string")
    return value.strip()


def _opt_str(value, field: str) -> str | None:
    if value is None:
        return None
    return _need_str(value, field)


class CaseService:
    def __init__(self, store: SQLiteStore):
        self.store = store

    # ---- 写 ----

    def create_case(self, *, case_id, actor, role, idempotency_key=None,
                    reason="") -> tuple[Case, Decision, bool]:
        """创建案件（仅提交者）。返回 (case, decision, created)。"""
        case_id = _need_str(case_id, "id")
        actor = _need_str(actor, "actor")
        role = _need_str(role, "role")
        key = _opt_str(idempotency_key, "idempotency_key")
        reason = reason if isinstance(reason, str) else ""
        require_role("create", role)
        with self.store.tx() as conn:
            if key:
                dup = self.store.decision_by_key(conn, key)
                if dup:
                    return self._replay(conn, dup)
            now = utcnow()
            case = Case(case_id, "draft", 1, actor, now, now)
            decision = Decision(case_id, 1, "create", "", "draft",
                                actor, role, reason, key, now)
            try:
                self.store.insert_case(conn, case)
                decision = self.store.insert_decision(conn, decision)
            except sqlite3.IntegrityError as exc:
                raise Conflict(f"case {case_id!r} already exists") from exc
            return case, decision, True

    def apply_action(self, *, case_id, action, actor, role,
                     idempotency_key=None, reason="",
                     expected_version=None) -> tuple[Case, Decision, bool]:
        """对案件应用一个动作。返回 (case, decision, applied)。"""
        case_id = _need_str(case_id, "id")
        action = _need_str(action, "action")
        actor = _need_str(actor, "actor")
        role = _need_str(role, "role")
        key = _opt_str(idempotency_key, "idempotency_key")
        reason = reason if isinstance(reason, str) else ""
        if action not in ACTIONS or action == "create":
            raise ValidationError(
                f"unknown action: {action!r}, expected one of "
                f"{[a for a in ACTIONS if a != 'create']}")
        if expected_version is not None and (
                not isinstance(expected_version, int) or expected_version < 1):
            raise ValidationError("expected_version must be a positive integer")
        require_role(action, role)
        with self.store.tx() as conn:
            if key:
                dup = self.store.decision_by_key(conn, key)
                if dup:
                    return self._replay(conn, dup)
            case = self.store.get_case(conn, case_id)
            if case is None:
                raise NotFound(f"case {case_id!r} not found")
            to_state = check_transition(action, case.state)
            if expected_version is not None and expected_version != case.version:
                raise Conflict(
                    f"expected_version {expected_version} does not match "
                    f"current version {case.version}")
            now = utcnow()
            decision = Decision(case_id, case.version + 1, action, case.state,
                                to_state, actor, role, reason, key, now)
            decision = self.store.insert_decision(conn, decision)
            if not self.store.advance_case(conn, case_id, case.version,
                                           to_state, now):
                raise Conflict("concurrent modification, please retry")
            return self.store.get_case(conn, case_id), decision, True

    def _replay(self, conn, dup: Decision) -> tuple[Case, Decision, bool]:
        """幂等重放：返回首次写入的决定与当前状态，不产生新记录。"""
        return self.store.get_case(conn, dup.case_id), dup, False

    # ---- 读 ----

    def get_case(self, case_id: str) -> Case:
        case = self.store.case(case_id)
        if case is None:
            raise NotFound(f"case {case_id!r} not found")
        return case

    def list_cases(self) -> list[Case]:
        return self.store.list_cases()

    def list_decisions(self, case_id: str) -> list[Decision]:
        self.get_case(case_id)  # 不存在则 404
        return self.store.list_decisions(case_id)
