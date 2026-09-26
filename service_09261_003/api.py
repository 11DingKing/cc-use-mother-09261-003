"""HTTP JSON API 适配器（仅标准库）。

路由：
  POST /cases                    创建案件（提交者）
  POST /cases/{id}/decisions     应用动作（submit/approve/reject/revise/publish）
  GET  /cases                    列出全部案件当前状态
  GET  /cases/{id}               单个案件当前状态
  GET  /cases/{id}/decisions     案件的全部历史决定（追加式，不可覆盖）
  GET  /healthz                  健康检查
"""
from __future__ import annotations

import json
import logging
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote, urlparse

from .service import CaseService
from .workflow import DomainError, ValidationError

log = logging.getLogger("case-service")


def _envelope(case, decision, applied: bool) -> dict:
    return {
        "case": asdict(case),
        "decision": asdict(decision),
        "idempotent_replay": not applied,
    }


class CaseHandler(BaseHTTPRequestHandler):
    server_version = "CaseService/1.0"
    service: CaseService = None  # 由 make_server 注入
    quiet = False

    # ---- 基础工具 ----

    def log_message(self, fmt, *args):  # noqa: A003 - 标准库签名
        if not self.quiet:
            super().log_message(fmt, *args)

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        if not raw:
            return {}
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ValidationError(f"invalid JSON body: {exc}") from exc
        if not isinstance(data, dict):
            raise ValidationError("request body must be a JSON object")
        return data

    def _segments(self) -> list[str]:
        path = urlparse(self.path).path
        return [unquote(s) for s in path.split("/") if s]

    # ---- 分发 ----

    def do_GET(self):
        self._handle("GET")

    def do_POST(self):
        self._handle("POST")

    def _handle(self, method: str) -> None:
        try:
            status, payload = self._route(method)
        except DomainError as exc:
            status, payload = exc.status, {"error": exc.code, "message": str(exc)}
        except Exception:  # pragma: no cover - 兜底
            log.exception("unhandled error")
            status, payload = 500, {"error": "internal", "message": "internal error"}
        self._send(status, payload)

    def _route(self, method: str) -> tuple[int, dict]:
        seg = self._segments()
        svc = self.service

        if method == "GET" and seg == ["healthz"]:
            return 200, {"status": "ok"}

        if method == "POST" and seg == ["cases"]:
            body = self._body()
            case, decision, created = svc.create_case(
                case_id=body.get("id"), actor=body.get("actor"),
                role=body.get("role"),
                idempotency_key=body.get("idempotency_key"),
                reason=body.get("reason", ""))
            return (201 if created else 200), _envelope(case, decision, created)

        if method == "GET" and seg == ["cases"]:
            return 200, {"cases": [asdict(c) for c in svc.list_cases()]}

        if len(seg) == 2 and seg[0] == "cases" and method == "GET":
            return 200, {"case": asdict(svc.get_case(seg[1]))}

        if len(seg) == 3 and seg[0] == "cases" and seg[2] == "decisions":
            if method == "GET":
                return 200, {"case_id": seg[1], "decisions": [
                    asdict(d) for d in svc.list_decisions(seg[1])]}
            if method == "POST":
                body = self._body()
                case, decision, applied = svc.apply_action(
                    case_id=seg[1], action=body.get("action"),
                    actor=body.get("actor"), role=body.get("role"),
                    idempotency_key=body.get("idempotency_key"),
                    reason=body.get("reason", ""),
                    expected_version=body.get("expected_version"))
                return (201 if applied else 200), _envelope(case, decision, applied)

        return 404, {"error": "not_found", "message": "route not found"}


def make_server(host: str, port: int, service: CaseService,
                quiet: bool = False) -> ThreadingHTTPServer:
    """构建多线程 HTTP 服务（每个请求一个线程，底层 SQLite 串行化写入）。"""
    handler = type("BoundCaseHandler", (CaseHandler,),
                   {"service": service, "quiet": quiet})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server
