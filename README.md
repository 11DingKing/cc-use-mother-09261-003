# 真实工程案例脱敏交付 —— 三权分立案件工作流服务

单容器、零第三方依赖（仅 Python 3.11 标准库）的案件状态工作流服务：
**提交者 / 复核者 / 发布者** 拥有不同动作，所有决定只追加、不可覆盖，
SQLite 文件持久化，HTTP/JSON 读写，支持乐观并发控制与幂等重试。

## 角色与状态机

| 角色 | 可执行动作 |
|---|---|
| `submitter` 提交者 | `create`（创建）、`submit`（送审）、`revise`（被退回后修订）、`cancel`（撤回） |
| `reviewer` 复核者 | `approve`（通过）、`reject`（退回） |
| `publisher` 发布者 | `publish`（发布）、`archive`（归档） |

```
draft ──submit──▶ reviewing ──approve──▶ approved ──publish──▶ published ──archive──▶ archived
  │                   │
  │                   └──reject──▶ rejected ──revise──▶ draft
  └──cancel──▶ cancelled            └──cancel──▶ cancelled
```

越权动作返回 `403 permission_denied`；状态不允许的动作返回 `422 invalid_transition`。

## 不可覆盖的决定日志

* `decisions` 表是 append-only 事实表：每次动作一行，版本号 = 决定序号。
* SQLite 触发器在**数据库层面**禁止 `UPDATE` / `DELETE`（报错
  `decisions are append-only`），DBA 直连也无法改写历次决定。
* 当前状态由决定日志回放（事件溯源）得到，`GET /cases/{id}` 返回完整历史。

## 并发语义

* 写操作在 `BEGIN IMMEDIATE` 事务 + 进程写锁内串行执行。
* 动作请求可带 `expected_version`（先 GET 取当前版本号）：版本不匹配返回
  `409 version_conflict`，客户端重读后重试。
* N 个客户端持同一版本并发时：恰好 1 个成功，其余 409，最终状态唯一、
  版本严格连续、不丢决定（见 `scripts/verify_concurrency.py`）。
* `idempotency_key` 支持安全重试：同键重复请求返回首次结果，不产生新决定。

## HTTP API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/healthz` | 存活探针 |
| POST | `/cases` | 提交者创建案件 |
| GET | `/cases` | 案件列表 |
| GET | `/cases/{id}` | 案件详情（含 `history` 决定流） |
| GET | `/cases/{id}/history` | 只追加决定流 |
| POST | `/cases/{id}/actions` | 追加一个角色动作 |

角色/操作者可放在 JSON 体（`actor`、`role`）或请求头
（`X-Actor`、`X-Role`）。

```bash
# 创建
curl -s -X POST localhost:8080/cases -H 'Content-Type: application/json' \
  -d '{"id":"c1","actor":"alice","role":"submitter"}'
# 送审（带乐观版本号）
curl -s -X POST localhost:8080/cases/c1/actions -H 'Content-Type: application/json' \
  -d '{"action":"submit","actor":"alice","role":"submitter","expected_version":1}'
# 复核
curl -s -X POST localhost:8080/cases/c1/actions -H 'Content-Type: application/json' \
  -d '{"action":"approve","actor":"bob","role":"reviewer"}'
# 发布
curl -s -X POST localhost:8080/cases/c1/actions -H 'Content-Type: application/json' \
  -d '{"action":"publish","actor":"carol","role":"publisher"}'
```

## 运行

### Docker（推荐，单容器 + 数据卷）

```bash
docker compose up --build -d
docker compose logs -f
# 停止后数据保留在命名卷 case-data 中
```

或直接 docker：

```bash
docker build -t case-workflow:latest .
docker run -d -p 8080:8080 -v case-data:/data case-workflow:latest
```

### 裸机 Python

```bash
CASE_DB_PATH=./data/cases.db PORT=8080 python3 -m service_09261_003.api
```

环境变量：`CASE_DB_PATH`（默认 `/data/cases.db`）、`HOST`（默认 `0.0.0.0`）、
`PORT`（默认 `8080`）。

## 测试与并发验证

```bash
# 单元 + HTTP 端到端测试（32 个）
python3 -m unittest discover -s tests -v
python3 -m compileall -q service_09261_003 tests

# 对运行中的服务打真实并发流量（默认 25 并发，17 项断言）
python3 scripts/verify_concurrency.py http://127.0.0.1:8080 25
```

## 目录结构

```
service_09261_003/
  workflow.py   # 状态机与角色分权（纯函数裁决 + 内存模型）
  store.py      # SQLite append-only 仓储、乐观并发、幂等键
  api.py        # ThreadingHTTPServer HTTP/JSON 边界
tests/          # 角色矩阵 / 仓储 / 持久化 / 并发 / HTTP 端到端
scripts/
  verify_concurrency.py  # 真实 HTTP 并发最终状态验证
Dockerfile / docker-compose.yml
```
