#!/usr/bin/env python3
"""端到端并发验证：对运行中的容器打真实 HTTP 流量。

验证内容：
1. 三权分立：submitter / reviewer / publisher 各自动作，越权返回 403。
2. 并发提交最终状态：N 个复核者同版本号并发 approve，恰好一个成功（409 其余），
   随后 N 个发布者并发 publish 同样收敛；最终状态唯一、版本连续、决定一条不丢。
3. 幂等键：同键重试不产生重复决定。
4. 只追加：历史包含全部动作，版本号严格连续。

用法：python3 scripts/verify_concurrency.py [base_url] [并发数]
退出码 0 表示全部断言通过。
"""
import json
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime

BASE = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://127.0.0.1:8080"
N = int(sys.argv[2]) if len(sys.argv) > 2 else 25

SUBMITTER, REVIEWER, PUBLISHER = "submitter", "reviewer", "publisher"
passed = []


def check(name, cond, detail=""):
    tag = "PASS" if cond else "FAIL"
    print(f"[{tag}] {name}" + (f" -- {detail}" if detail and not cond else ""))
    if not cond:
        sys.exit(1)
    passed.append(name)


def request(method, path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode())


def concurrent(step, fn, n):
    """n 个线程在同一屏障释放后并发执行 fn(i)，收集返回值列表。"""
    results = [None] * n
    barrier = threading.Barrier(n)

    def worker(i):
        barrier.wait()
        try:
            results[i] = fn(i)
        except Exception as exc:  # pragma: no cover
            results[i] = ("error", repr(exc))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    print(f"-- {step}: 并发 {n} 个请求 ...")
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return results


def main():
    print(f"目标: {BASE}  并发度: {N}  时间: {datetime.now().isoformat(timespec='seconds')}")

    status, body = request("GET", "/healthz")
    check("服务存活", status == 200 and body.get("status") == "ok")

    case_id = f"cc-{datetime.now().strftime('%H%M%S')}"

    # 1) 提交者创建 + 送审
    status, body = request("POST", "/cases",
                           {"id": case_id, "actor": "alice", "role": SUBMITTER})
    check("提交者创建案件 -> 201 draft",
          status == 201 and body["case"]["state"] == "draft")
    status, body = request("POST", f"/cases/{case_id}/actions",
                           {"action": "submit", "actor": "alice",
                            "role": SUBMITTER, "expected_version": 1})
    check("提交者送审 -> reviewing v2",
          status == 200 and body["case"]["version"] == 2
          and body["case"]["state"] == "reviewing")

    # 2) 越权动作被拒绝
    status, body = request("POST", f"/cases/{case_id}/actions",
                           {"action": "approve", "actor": "alice",
                            "role": SUBMITTER})
    check("提交者不能复核 -> 403", status == 403, body.get("error"))
    status, body = request("POST", f"/cases/{case_id}/actions",
                           {"action": "publish", "actor": "bob",
                            "role": REVIEWER})
    check("复核者不能发布 -> 403", status == 403, body.get("error"))

    # 3) N 个复核者并发 approve，全部基于 v2
    codes = concurrent("复核并发", lambda i: request(
        "POST", f"/cases/{case_id}/actions",
        {"action": "approve", "actor": f"rev-{i}", "role": REVIEWER,
         "expected_version": 2})[0], N)
    check("并发 approve 恰好 1 个成功",
          codes.count(200) == 1, f"200x{codes.count(200)} / 409x{codes.count(409)}")
    check("其余全部版本冲突 409",
          codes.count(409) == N - 1, f"got {sorted(set(codes))}")

    status, body = request("GET", f"/cases/{case_id}")
    check("approve 后状态为 approved v3",
          body["state"] == "approved" and body["version"] == 3,
          json.dumps({"state": body["state"], "version": body["version"]}))

    # 4) N 个发布者并发 publish，全部基于 v3
    codes = concurrent("发布并发", lambda i: request(
        "POST", f"/cases/{case_id}/actions",
        {"action": "publish", "actor": f"pub-{i}", "role": PUBLISHER,
         "expected_version": 3})[0], N)
    check("并发 publish 恰好 1 个成功",
          codes.count(200) == 1, f"200x{codes.count(200)} / 409x{codes.count(409)}")
    check("publish 其余全部 409", codes.count(409) == N - 1)

    # 5) 最终状态校验：唯一、连续、只追加
    status, body = request("GET", f"/cases/{case_id}")
    versions = [d["version"] for d in body["history"]]
    actions = [d["action"] for d in body["history"]]
    check("最终状态 published，版本 4",
          body["state"] == "published" and body["version"] == 4,
          json.dumps({"state": body["state"], "version": body["version"]}))
    check("决定版本严格连续 1..4", versions == [1, 2, 3, 4], str(versions))
    check("历次决定全部保留未被覆盖",
          actions == ["create", "submit", "approve", "publish"], str(actions))
    check("每个决定保留了真实操作者与角色",
          all(d["actor"] and d["role"] for d in body["history"]))

    # 6) 幂等键：同键重复送审一个新案件，不产生新版本
    cid2 = case_id + "-idem"
    payload = {"id": cid2, "actor": "alice", "role": SUBMITTER,
               "idempotency_key": f"key-{cid2}"}
    s1, b1 = request("POST", "/cases", payload)
    s2, b2 = request("POST", "/cases", payload)
    check("幂等创建：首次 201、重放 200、版本不变",
          s1 == 201 and s2 == 200 and b2["idempotent_replay"]
          and b1["case"]["version"] == b2["case"]["version"] == 1)

    act_payload = {"action": "submit", "actor": "alice", "role": SUBMITTER,
                   "idempotency_key": f"key-{cid2}-submit"}
    request("POST", f"/cases/{cid2}/actions", act_payload)
    s3, b3 = request("POST", f"/cases/{cid2}/actions", act_payload)
    check("幂等动作：重放不产生新版本",
          s3 == 200 and b3["idempotent_replay"]
          and b3["case"]["version"] == 2)

    # 7) 终态不可再迁移
    status, body = request("POST", f"/cases/{case_id}/actions",
                           {"action": "approve", "actor": "bob",
                            "role": REVIEWER})
    check("published 上再 approve -> 422", status == 422, body.get("error"))

    print(f"\n全部通过：{len(passed)} 项断言。案件 {case_id} 最终状态 = published@v4")


if __name__ == "__main__":
    main()
