"""版本化业务工作流：提交者 / 复核者 / 发布者三权分立，动作驱动状态迁移。

迁移图::

    draft ──submit──▶ reviewing ──approve──▶ approved ──publish──▶ published ──archive──▶ archived
      │                   │
      │                   └──reject──▶ rejected ──revise──▶ draft
      └──cancel──▶ cancelled            └──cancel──▶ cancelled

旧版 ``Workflow`` / ``Case`` 内存模型保留，供既有测试与离线推演使用；
HTTP 服务的真实写路径在 :mod:`service_09261_003.store` 中，
动作合法性由本模块的 :func:`resolve` 统一裁决。
"""
from dataclasses import dataclass, asdict

# 角色
SUBMITTER = "submitter"   # 提交者
REVIEWER = "reviewer"     # 复核者
PUBLISHER = "publisher"   # 发布者
ROLES = (SUBMITTER, REVIEWER, PUBLISHER)

# 动作 -> (允许执行的角色, {当前状态: 目标状态})
ACTIONS = {
    "submit":  (SUBMITTER, {"draft": "reviewing"}),
    "revise":  (SUBMITTER, {"rejected": "draft"}),
    "cancel":  (SUBMITTER, {"draft": "cancelled", "rejected": "cancelled"}),
    "approve": (REVIEWER,  {"reviewing": "approved"}),
    "reject":  (REVIEWER,  {"reviewing": "rejected"}),
    "publish": (PUBLISHER, {"approved": "published"}),
    "archive": (PUBLISHER, {"published": "archived"}),
}
# 创建动作单独登记：它产生 version=1 的第一条决定
CREATE_ACTION = "create"
CREATE_ROLE = SUBMITTER


class WorkflowError(ValueError):
    """工作流领域错误基类。"""


class PermissionDenied(WorkflowError):
    """角色无权执行该动作。"""


class InvalidTransition(WorkflowError):
    """当前状态不允许该动作。"""


def resolve(action, state, role):
    """裁决动作是否合法，返回目标状态；否则抛 :class:`WorkflowError`。"""
    rule = ACTIONS.get(action)
    if rule is None:
        raise InvalidTransition(f"unknown action: {action}")
    required_role, moves = rule
    if role != required_role:
        raise PermissionDenied(
            f"role '{role}' cannot perform '{action}', requires '{required_role}'"
        )
    to_state = moves.get(state)
    if to_state is None:
        raise InvalidTransition(f"action '{action}' invalid in state '{state}'")
    return to_state


@dataclass(frozen=True)
class Case:
    id: str
    actor: str
    state: str
    version: int = 1

    def move(self, state, actor):
        allowed = {
            "draft": {"reviewing", "cancelled"},
            "reviewing": {"approved", "rejected"},
            "rejected": {"draft", "cancelled"},
            "approved": {"published", "archived"},
            "published": {"archived"},
        }
        if state not in allowed.get(self.state, set()):
            raise ValueError("invalid transition")
        return Case(self.id, actor, state, self.version + 1)

    def to_dict(self):
        return asdict(self)


class Workflow:
    """内存版工作流（仅用于离线推演与既有单元测试）。"""

    def __init__(self):
        self.rows = {}
        self.keys = {}

    def create(self, id, actor, key=None):
        if key in self.keys:
            return self.rows[self.keys[key]]
        if id in self.rows:
            raise ValueError("duplicate")
        row = Case(id, actor, "draft")
        self.rows[id] = row
        if key:
            self.keys[key] = id
        return row

    def move(self, id, state, actor):
        self.rows[id] = self.rows[id].move(state, actor)
        return self.rows[id]

    def snapshot(self):
        return [asdict(self.rows[k]) for k in sorted(self.rows)]
