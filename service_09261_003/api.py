"""HTTP/JSON 边界：三权分立的案件工作流读写 API。

路由::

    GET    /healthz                  存活探针
    POST   /cases                    提交者创建案件
    GET    /cases                    案件列表（决定日志回放的投影）
    GET    /cases/{id}               案件详情（含完整决定历史）
    GET    /cases/{id}/history       只追加决定流
    POST   /cases/{id}/actions       追加一个角色动作

角色与动作：

* submitter 提交者：submit / revise / cancel
* reviewer  复核者：approve / reject
* publisher 发布者：publish / archive

并发控制：POST 动作可带 ``expected_version``（通常先 GET 取版本号）。
版本不匹配返回 ``409 version_conflict``，客户端重读后重试。

服务仅依赖 Python 标准库，可直接 ``python -m service_09261_003.api`` 运行。
"""
from __future__ import annotations

import json
import re
import signal
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .store import DuplicateCase, SQLiteStore, VersionConflict
from .workflow import ACTIONS, ROLES, InvalidTransition, PermissionDenied

CASE_RE = re.compile(r"^/cases/([^/]+)$")
ACTION_RE = re.compile(r"^/cases/([^/]+)/actions$")
HISTORY_RE = re.compile(r"^/cases/([^/]+)/history$")

# 动作 -> 允许角色（与 workflow.ACTIONS 保持一致，暴露给 API 使用者）
ACTION_ROLES = {action: rule[0] for action, rule in ACTIONS.items()}


def make_handler(store: SQLiteStore):
    class Handler(BaseHTTPRequestHandler):
        server_version = "CaseWorkflow/1.0"

        def log_message(self, fmt, *args):
            # 单行访问日志，便于容器采集（切勿调用 log_error，会递归）
            sys.stderr.write(
                "%s - - [%s] %s\n"
                % (self.address_string(), self.log_date_time_string(),
                   fmt % args)
            )

        # ------------------------------------------------------------ #
        # 基础工具
        # ------------------------------------------------------------ #
        def _send_json(self, status, obj):
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (json.JSONDecodeError, UnicodeDecodeError):
                raise _BadRequest("invalid JSON body")
            if not isinstance(data, dict):
                raise _BadRequest("JSON body must be an object")
            return data

        def _actor_role(self, body):
            actor = body.get("actor") or self.headers.get("X-Actor")
            role = body.get("role") or self.headers.get("X-Role")
            if not actor:
                raise _BadRequest("'actor' is required")
            if not role:
                raise _BadRequest("'role' is required")
            if role not in ROLES:
                raise _BadRequest(f"'role' must be one of {list(ROLES)}")
            return actor, role

        def _domain_error(self, exc):
            if isinstance(exc, PermissionDenied):
                return 403, "permission_denied"
            if isinstance(exc, VersionConflict):
                return 409, "version_conflict"
            if isinstance(exc, DuplicateCase):
                return 409, "duplicate_case"
            if isinstance(exc, InvalidTransition):
                msg = str(exc)
                if "not found" in msg:
                    return 404, "not_found"
                return 422, "invalid_transition"
            return 500, "internal_error"

        # ------------------------------------------------------------ #
        # GET
        # ------------------------------------------------------------ #
        def do_GET(self):
            path = self.path.split("?", 1)[0]
            try:
                if path == "/healthz":
                    self._send_json(200, {"status": "ok"})
                    return
                if path == "/cases":
                    self._send_json(200, {"cases": store.list_cases()})
                    return
                m = HISTORY_RE.match(path)
                if m:
                    case = store.get_case(m.group(1))
                    if case is None:
                        self._send_json(404, {"error": "not_found"})
                        return
                    self._send_json(200, {
                        "id": case["id"], "version": case["version"],
                        "decisions": case["history"],
                    })
                    return
                m = CASE_RE.match(path)
                if m:
                    case = store.get_case(m.group(1))
                    if case is None:
                        self._send_json(404, {"error": "not_found"})
                        return
                    self._send_json(200, case)
                    return
                self._send_json(404, {"error": "not_found"})
            except Exception as exc:  # pragma: no cover - 防御性
                self._send_json(500, {"error": "internal_error", "detail": str(exc)})

        # ------------------------------------------------------------ #
        # POST
        # ------------------------------------------------------------ #
        def do_POST(self):
            path = self.path.split("?", 1)[0]
            try:
                body = self._read_json()
                if path == "/cases":
                    actor, role = self._actor_role(body)
                    case_id = body.get("id")
                    if not case_id or not isinstance(case_id, str):
                        raise _BadRequest("'id' is required")
                    case, replayed = store.create_case(
                        case_id, actor, role=role,
                        idempotency_key=body.get("idempotency_key"),
                        payload=body.get("payload"),
                    )
                    self._send_json(201 if not replayed else 200,
                                    {"case": case, "idempotent_replay": replayed})
                    return

                m = ACTION_RE.match(path)
                if m:
                    case_id = m.group(1)
                    actor, role = self._actor_role(body)
                    action = body.get("action")
                    if not action:
                        raise _BadRequest("'action' is required")
                    if action not in ACTION_ROLES:
                        raise _BadRequest(
                            f"'action' must be one of {sorted(ACTION_ROLES)}"
                        )
                    expected = body.get("expected_version")
                    if expected is not None and (
                        not isinstance(expected, int) or isinstance(expected, bool)
                    ):
                        raise _BadRequest("'expected_version' must be an integer")
                    case, replayed = store.act(
                        case_id, action, actor, role,
                        expected_version=expected,
                        idempotency_key=body.get("idempotency_key"),
                        payload=body.get("payload"),
                    )
                    self._send_json(200, {"case": case, "idempotent_replay": replayed})
                    return

                self._send_json(404, {"error": "not_found"})
            except _BadRequest as exc:
                self._send_json(400, {"error": "bad_request", "detail": str(exc)})
            except Exception as exc:
                status, code = self._domain_error(exc)
                self._send_json(status, {"error": code, "detail": str(exc)})

    return Handler


class _BadRequest(Exception):
    """请求体不合法。"""


def create_server(host, port, store):
    httpd = ThreadingHTTPServer((host, port), make_handler(store))
    httpd.daemon_threads = True
    return httpd


def main(argv=None):
    import os

    db_path = os.environ.get("CASE_DB_PATH", "/data/cases.db")
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))

    parent = os.path.dirname(db_path)
    if parent and parent != ":memory:":
        os.makedirs(parent, exist_ok=True)
    store = SQLiteStore(db_path)
    httpd = create_server(host, port, store)

    def _shutdown(*_):
        # shutdown() 不能在 serve_forever 所在线程内直接调用，放到独立线程
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, _shutdown)
    signal.signal(signal.SIGINT, _shutdown)

    print(f"case-workflow listening on http://{host}:{port} db={db_path}",
          file=sys.stderr, flush=True)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        httpd.server_close()
        store.close()


if __name__ == "__main__":
    main()
