# 真实工程案例脱敏交付

纯 Python（仅标准库）的单容器服务端应用：版本化案件流转、角色分权、
追加式决定日志、幂等命令、SQLite 持久化与 JSON HTTP API。

## 角色与动作

| 角色 | 动作 | 状态流转 |
| --- | --- | --- |
| submitter（提交者） | `create` / `submit` / `revise` | → `draft`；`draft → reviewing`；`rejected → draft` |
| reviewer（复核者） | `approve` / `reject` | `reviewing → approved`；`reviewing → rejected` |
| publisher（发布者） | `publish` | `approved → published`（终态） |

越权返回 `403`，非法流转/版本冲突返回 `409`，参数错误返回 `400`。

## 决定不可覆盖

每次动作在单个事务内写入 `decisions` 表并推进 `cases` 当前状态：

- `decisions` 表由触发器禁止 `UPDATE`/`DELETE`（追加式日志）；
- `UNIQUE(case_id, version)` 保证同一案件的决定序列不分叉；
- `idempotency_key` 唯一索引：重复请求重放首次结果，不产生新记录；
- 写操作走 `BEGIN IMMEDIATE` 事务 + 乐观版本检查，并发安全。

## HTTP API

```
POST /cases                   {"id","actor","role":"submitter","idempotency_key"?}
POST /cases/{id}/decisions    {"action","actor","role","expected_version"?,"idempotency_key"?,"reason"?}
GET  /cases                   全部案件当前状态
GET  /cases/{id}              单个案件当前状态
GET  /cases/{id}/decisions    该案件全部历史决定（按版本有序）
GET  /healthz                 健康检查
```

写接口响应：`{"case": {...}, "decision": {...}, "idempotent_replay": bool}`，
新决定返回 `201`，幂等重放返回 `200`。

## 运行

本地：

```bash
python3 -m service_09261_003            # HOST/PORT/DB_PATH 可配，默认 0.0.0.0:8080，./cases.db
```

单容器：

```bash
docker build -t case-service .
docker run -p 8080:8080 -v case-data:/data case-service
```

示例：

```bash
curl -s -X POST localhost:8080/cases \
  -d '{"id":"c1","actor":"alice","role":"submitter","idempotency_key":"k1"}'
curl -s -X POST localhost:8080/cases/c1/decisions \
  -d '{"action":"submit","actor":"alice","role":"submitter"}'
curl -s -X POST localhost:8080/cases/c1/decisions \
  -d '{"action":"approve","actor":"bob","role":"reviewer"}'
curl -s -X POST localhost:8080/cases/c1/decisions \
  -d '{"action":"publish","actor":"carol","role":"publisher"}'
curl -s localhost:8080/cases/c1/decisions   # 查看不可篡改的决定历史
```

## 测试与并发验证

```bash
python3 -m unittest discover -s tests -v
python3 -m compileall -q service_09261_003 tests
```

并发用例（`TestConcurrency`）对真实 HTTP 服务发起多线程请求，验证最终状态：

- 相同幂等键并发创建 → 恰好 1 个案件、1 条决定；
- 同 id 不同键并发创建 → 恰好 1 个 `201`，其余 `409`；
- 复核动作并发竞争 → 恰好 1 个生效，最终状态与决定历史一致；
- 多案件并发创建+提交 → 快照完整，历史链 `from_state/to_state` 逐环相扣、
  版本连续且等于决定数。
