#!/usr/bin/env python3
"""pi-py 综合负载压测（贴近真实 + 细粒度指标）。

两大块：
A. 客户端打负载 —— 6 类真实工作场景、多轮、随机 prompt 变体、思考停顿。
B. 服务端抓指标 —— /metrics 前后快照，算出模型/工具/沙箱/记忆 各阶段延迟 + token + 失败。

用法:
  python3 /root/load_test.py --users 20 --duration 120 --think 2.0

输出: 客户端吞吐/延迟分位/错误率 + 服务端几十项指标分解。
"""

import argparse
import asyncio
import json
import random
import re
import statistics
import sys
import time
from collections import defaultdict

import httpx

BASE = "http://127.0.0.1:8300"
METRICS_TOKEN = ""


def _load_token():
    global METRICS_TOKEN
    try:
        for line in open("/etc/pi.env"):
            if line.startswith("PI_METRICS_TOKEN="):
                METRICS_TOKEN = line.split("=", 1)[1].strip()
    except Exception:
        pass


# ---- 场景库（6 类，prompt 模板 + 随机参数）----------------------------------
_POOL = {
    "dep": ["销售", "技术", "人事", "财务", "市场"],
    "subject": ["项目进度同步", "季度总结", "请假申请", "预算申请", "会议纪要"],
    "style": ["更正式", "更口语化", "更简洁", "更委婉"],
    "concept": ["微服务架构", "向量数据库", "沙箱隔离", "RAG 检索增强生成", "限流与熔断"],
    "topic": ["远程办公效率", "企业知识管理", "AI 客服", "数据安全合规", "DevOps 实践"],
    "algo": ["快速排序", "二分查找", "斐波那契数列", "冒泡排序", "哈希表"],
    "code": "def foo(x):\n    return [i for i in x if i % 2 == 0] + 'abc'\n\nprint(foo([1,2,3,4]))",
    "ds": ["栈", "队列", "链表", "二叉树"],
    "n": ["10", "20", "50", "100"],
    "proj": ["demo-app", "my-service", "data-pipeline", "web-crawler"],
    "text": "企业级 AI 服务需要在保证沙箱隔离的同时维持低延迟，并且要有完善的审计与计量能力。",
}

SCENARIOS = {
    "qa": [
        "帮我写一封给{dep}部门的邮件，主题是「{subject}」，100 字以内，直接输出邮件正文",
        "把下面这段话改写得{style}：{text}",
        "用通俗的话解释一下「{concept}」，150 字以内",
        "给我列 5 条关于「{topic}」的要点，每条一句话",
    ],
    "code": [
        "写一个 Python 函数实现{algo}，并用 3 个测试用例验证，输出结果",
        "下面这段代码有 bug，帮我找出并修复：\n```python\n{code}\n```",
        "用 bash 写个脚本读取 CSV 并统计每列空值数量，然后跑一下演示",
        "实现一个{ds}数据结构（增删查），给一个使用示例并运行",
    ],
    "data": [
        "用 bash 生成一份 {n} 行的销售数据 CSV（列：date,product,amount），用 pandas 求总金额和按 product 分组汇总，只回关键数字",
        "生成 {n} 条学生成绩数据，算平均分/最高分/最低分，找出不及格人数",
    ],
    "doc": [
        "总结下面这段文档的 3 个要点：{text}",
        "把下面这段文字翻译成英文并润色：{text}",
    ],
    "rag": [
        "用 rag_search 查询「{topic}」相关的知识库内容，总结要点",
    ],
    "multi": [
        "创建名为 {proj} 的项目目录，含 README.md 和 main.py（main.py 写 hello world），然后运行 main.py 验证",
    ],
}
WEIGHTS = {"qa": 30, "code": 25, "data": 20, "doc": 15, "rag": 5, "multi": 5}

# 轮询的模型（deepseek 权重最高，另两家兜底）
MODELS = ["openai/deepseek-flash"]
MODEL_WEIGHTS = [1]


def make_prompt(typ):
    tmpl = random.choice(SCENARIOS[typ])
    def rep(m):
        key = m.group(1)
        pool = _POOL[key]
        v = random.choice(pool) if isinstance(pool, list) else pool
        return str(v)
    return re.sub(r"\{(\w+)\}", rep, tmpl)


# ---- Prometheus 指标解析 ---------------------------------------------------
def scrape_metrics() -> dict:
    """返回 {metric_name: [(labels_dict, value), ...]}"""
    try:
        r = httpx.get(f"{BASE}/metrics", headers={"Authorization": f"Bearer {METRICS_TOKEN}"}, timeout=10)
        text = r.text
    except Exception:
        return {}
    out = defaultdict(list)
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        m = re.match(r"^(\w+)(?:\{(.*?)\})?\s+([-0-9.eE+]+)", line)
        if not m:
            continue
        name, labels_str, val = m.groups()
        labels = {}
        if labels_str:
            for kv in labels_str.split(","):
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    labels[k] = v.strip('"')
        out[name].append((labels, float(val)))
    return dict(out)


def metric_sum(metrics, name, label_filter=None):
    """sum of a counter/histogram metric, optionally filtered by labels."""
    total = 0.0
    for labels, val in metrics.get(name, []):
        if label_filter and not all(labels.get(k) == v for k, v in label_filter.items()):
            continue
        total += val
    return total


def metric_by(metrics, name, key):
    """breakdown of a metric by a label key."""
    out = defaultdict(float)
    for labels, val in metrics.get(name, []):
        out[labels.get(key, "")] += val
    return dict(out)


# ---- 客户端压测 ------------------------------------------------------------
async def one_user(uid, results, stop, password, think):
    try:
        async with httpx.AsyncClient(base_url=BASE, timeout=120) as c:
            r = await c.post("/v1/auth/login", json={"username": f"load-{uid}", "password": password})
            tok = r.json()["access_token"]
            H = {"Authorization": f"Bearer {tok}"}
            r = await c.post("/v1/sessions", json={"title": f"压测-{uid}"}, headers=H)
            sid = r.json()["id"]

            while not stop.is_set():
                typ = random.choices(list(WEIGHTS), weights=list(WEIGHTS.values()))[0]
                model = random.choices(MODELS, weights=MODEL_WEIGHTS)[0]
                t0 = time.perf_counter()
                try:
                    prompt = make_prompt(typ)
                    async with c.stream("POST", f"/v1/sessions/{sid}/runs", headers=H,
                                        timeout=120, json={"prompt": prompt, "model": model}) as resp:
                        async for _ in resp.aiter_lines():
                            pass
                    dt = time.perf_counter() - t0
                    results.append((typ, model, "ok", dt))
                except Exception as e:
                    dt = time.perf_counter() - t0
                    results.append((typ, model, "err", dt, f"{type(e).__name__}: {e}"[:80]))
                # 真实用户有思考停顿
                if think > 0 and not stop.is_set():
                    await asyncio.sleep(random.uniform(0.5 * think, 1.5 * think))
    except Exception as e:
        results.append(("setup", "", "err", 0.0, str(e)[:60]))


def _pct(lat, p):
    if not lat:
        return 0.0
    lat = sorted(lat)
    return lat[min(len(lat) - 1, int(len(lat) * p))]


def client_report(results, duration, users):
    oks = [r for r in results if r[2] == "ok"]
    errs = [r for r in results if r[2] == "err"]
    lat = [r[3] for r in oks]
    total = len(results)
    print(f"\n========== 客户端指标（并发 {users}，时长 {duration:.0f}s）==========")
    if total == 0:
        print("  无数据")
        return
    print(f"  总轮次: {total}   成功: {len(oks)}   失败: {len(errs)}   错误率: {len(errs)/total*100:.2f}%")
    print(f"  吞吐: {len(oks)/duration*60:.1f} 轮/分钟")
    print(f"  端到端延迟: P50={_pct(lat,0.50):.2f}s  P90={_pct(lat,0.90):.2f}s  P95={_pct(lat,0.95):.2f}s  P99={_pct(lat,0.99):.2f}s  平均={statistics.mean(lat):.2f}s")
    by = defaultdict(lambda: [0, 0, []])
    for r in results:
        by[r[0]][0 if r[2] == "ok" else 1] += 1
        if r[2] == "ok":
            by[r[0]][2].append(r[3])
    print("  ---- 各场景 ----")
    for t in ("qa", "code", "data", "doc", "rag", "multi"):
        ok, er, lat2 = by[t]
        if ok or er:
            avg = statistics.mean(lat2) if lat2 else 0
            print(f"    {t:6s}: {ok:4d} ok / {er:2d} err   平均 {avg:.2f}s")
    # 各模型分布
    mby = defaultdict(lambda: [0, []])
    for r in oks:
        mby[r[1]][0] += 1
        mby[r[1]][1].append(r[3])
    print("  ---- 各模型 ----")
    for m, (n, lat2) in sorted(mby.items(), key=lambda x: -x[1][0]):
        avg = statistics.mean(lat2) if lat2 else 0
        print(f"    {m:28s}: {n:4d} 轮   平均 {avg:.2f}s")
    if errs:
        print("  错误样例:", [e[4] for e in errs[:3]])


def server_report(before, after):
    print(f"\n========== 服务端指标（/metrics 前后快照增量）==========")

    def delta(name, lf=None):
        return metric_sum(after, name, lf) - metric_sum(before, name, lf)

    def avg(name):
        d_count = delta(name + "_count")
        d_sum = delta(name + "_sum")
        return d_sum / d_count if d_count else 0.0

    # 模型
    n_llm = delta("pi_llm_calls_total")
    n_llm_fail = delta("pi_llm_calls_total", {"ok": "false"})
    print(f"  模型调用: {n_llm:.0f} 次（失败 {n_llm_fail:.0f}）   平均延迟 {avg('pi_llm_call_duration_seconds'):.2f}s")
    # 工具/沙箱
    n_tool = delta("pi_tool_calls_total")
    print(f"  工具执行: {n_tool:.0f} 次   平均延迟 {avg('pi_tool_call_duration_seconds'):.2f}s")
    for t, v in sorted(metric_by(after, "pi_tool_calls_total", "tool").items(), key=lambda x: -x[1]):
        b = metric_by(before, "pi_tool_calls_total", "tool").get(t, 0)
        if v - b > 0:
            print(f"      {t}: {v-b:.0f} 次")
    # 沙箱
    n_sbx = delta("pi_sandbox_create_duration_seconds_count")
    if n_sbx:
        print(f"  沙箱创建: {n_sbx:.0f} 次   平均延迟 {avg('pi_sandbox_create_duration_seconds'):.2f}s")
    print(f"  沙箱失败: {delta('pi_sandbox_create_failures_total'):.0f}   命令超时: {delta('pi_sandbox_command_timeouts_total'):.0f}   池命中: {delta('pi_sandbox_pool_hits_total'):.0f}")
    # 记忆/知识库
    n_mem = delta("pi_memory_retrieve_duration_seconds_count")
    if n_mem:
        print(f"  记忆/知识库召回: {n_mem:.0f} 次   平均延迟 {avg('pi_memory_retrieve_duration_seconds'):.2f}s")
        for k, v in sorted(metric_by(after, "pi_memory_retrievals_total", "outcome").items()):
            b = metric_by(before, "pi_memory_retrievals_total", "outcome").get(k, 0)
            if v - b > 0:
                print(f"      结果 {k}: {v-b:.0f} 次")
    # token
    toks = metric_by(after, "pi_tokens_total", "direction")
    toks_b = metric_by(before, "pi_tokens_total", "direction")
    for d in ("input", "output"):
        n = toks.get(d, 0) - toks_b.get(d, 0)
        if n:
            print(f"  Token {d}: {n:.0f}")
    # run
    n_run = delta("pi_runs_total")
    print(f"  Run: {n_run:.0f} 个   平均时长 {avg('pi_run_duration_seconds'):.2f}s   平均轮数 {delta('pi_run_turns_sum')/n_run if n_run else 0:.1f}")
    # 降级/HTTP
    print(f"  模型降级(fallback): {delta('pi_llm_fallbacks_total'):.0f}")
    print(f"  HTTP 请求: {delta('pi_http_requests_total'):.0f}   TTFB 平均 {avg('pi_http_request_duration_seconds'):.2f}s")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--users", type=int, default=20)
    ap.add_argument("--duration", type=int, default=120)
    ap.add_argument("--think", type=float, default=2.0, help="思考停顿（秒），0=无停顿打满")
    ap.add_argument("--password", default="load-pass-123")
    args = ap.parse_args()
    _load_token()

    print(f"启动 {args.users} 并发用户，时长 {args.duration}s，思考停顿 {args.think}s ...")
    before = scrape_metrics()
    results = []
    stop = asyncio.Event()
    tasks = [asyncio.create_task(one_user(i, results, stop, args.password, args.think))
             for i in range(1, args.users + 1)]

    t0 = time.perf_counter()
    while True:
        await asyncio.sleep(10)
        elapsed = time.perf_counter() - t0
        oks = sum(1 for r in results if r[2] == "ok")
        print(f"  [{elapsed:5.0f}s] 完成 {len(results)} 轮（成功 {oks}）", flush=True)
        if elapsed >= args.duration:
            break
    load_window = time.perf_counter() - t0   # 实际压测窗口（不含排空）
    stop.set()
    await asyncio.gather(*tasks, return_exceptions=True)
    total_time = time.perf_counter() - t0     # 含排空在飞请求
    after = scrape_metrics()

    client_report(results, load_window, args.users)
    server_report(before, after)


if __name__ == "__main__":
    asyncio.run(main())
