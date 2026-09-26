"""版本化业务工作流：角色、动作与状态机（纯领域逻辑，不依赖 IO）。

三种角色拥有不同动作：
- submitter（提交者）：create / submit / revise
- reviewer（复核者）：approve / reject
- publisher（发布者）：publish

每一次动作都会沉淀为一条不可覆盖的 Decision（追加式日志），
Case 只是全部决定折叠后的当前视图。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

ROLES = ("submitter", "reviewer", "publisher")

# action -> (required_role, allowed_from_states, to_state)
ACTIONS = {
    "create": ("submitter", frozenset(), "draft"),
    "submit": ("submitter", frozenset({"draft"}), "reviewing"),
    "revise": ("submitter", frozenset({"rejected"}), "draft"),
    "approve": ("reviewer", frozenset({"reviewing"}), "approved"),
    "reject": ("reviewer", frozenset({"reviewing"}), "rejected"),
    "publish": ("publisher", frozenset({"approved"}), "published"),
}

TERMINAL_STATES = frozenset({"published"})


class DomainError(Exception):
    """业务错误基类，status/code 供 HTTP 层映射。"""

    status = 400
    code = "bad_request"


class ValidationError(DomainError):
    status = 400
    code = "validation"


class NotFound(DomainError):
    status = 404
    code = "not_found"


class Forbidden(DomainError):
    status = 403
    code = "forbidden"


class Conflict(DomainError):
    status = 409
    code = "conflict"


@dataclass(frozen=True)
class Case:
    id: str
    state: str
    version: int  # 等于该 case 已应用的决定数
    created_by: str
    created_at: str
    updated_at: str


@dataclass(frozen=True)
class Decision:
    case_id: str
    version: int  # 在本 case 决定序列中的序号，从 1 开始
    action: str
    from_state: str
    to_state: str
    actor: str
    role: str
    reason: str
    idempotency_key: str | None
    decided_at: str
    seq: int | None = None  # 全局单调序号，由数据库分配


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def require_role(action: str, role: str) -> None:
    if role not in ROLES:
        raise ValidationError(f"unknown role: {role!r}, expected one of {ROLES}")
    needed = ACTIONS[action][0]
    if role != needed:
        raise Forbidden(f"action {action!r} requires role {needed!r}, got {role!r}")


def check_transition(action: str, from_state: str) -> str:
    """校验状态机流转，返回目标状态。"""
    _, allowed, to_state = ACTIONS[action]
    if from_state not in allowed:
        raise Conflict(f"cannot {action} from state {from_state!r}")
    return to_state
