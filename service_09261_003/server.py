"""进程入口：python -m service_09261_003

环境变量：
  HOST     监听地址（默认 0.0.0.0）
  PORT     监听端口（默认 8080）
  DB_PATH  SQLite 文件路径（默认 ./cases.db）
"""
from __future__ import annotations

import os

from .api import make_server
from .service import CaseService
from .store import SQLiteStore


def main() -> None:
    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", "8080"))
    db_path = os.environ.get("DB_PATH", "cases.db")
    service = CaseService(SQLiteStore(db_path))
    server = make_server(host, port, service)
    print(f"case-service listening on {host}:{port}, db={db_path}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
