#!/usr/bin/env python3
"""无头浏览器截图：登录态注入 → 打开 pi 前端真实页面 → 批量出宣传图。

用法（root，chromium 已由 `playwright install chromium` + `install-deps` 装好）：
    sudo /opt/pi-venv/bin/python tools/screenshot_demo.py

依赖临时文件：/tmp/smoketest_token（JWT）、/tmp/smoketest_username、
/tmp/smoketest_sid（会话 id）。输出目录 /home/ubuntu/shots/，PNG @2x 高清。
"""
from __future__ import annotations

import asyncio
import os
import sys

from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:8300"
OUT = "/home/ubuntu/shots"
VIEWPORT = {"width": 1440, "height": 900}


def _read(p: str) -> str:
    with open(p, encoding="utf-8") as f:
        return f.read().strip()


async def main() -> int:
    token = _read("/tmp/smoketest_token")
    username = _read("/tmp/smoketest_username")
    sid = _read("/tmp/smoketest_sid")
    os.makedirs(OUT, exist_ok=True)

    async with async_playwright() as p:
        browser = await p.chromium.launch(args=["--no-sandbox"])
        ctx = await browser.new_context(
            viewport=VIEWPORT,
            device_scale_factor=2,  # 高清 2x，适合宣传图
            color_scheme="light",
        )
        page = await ctx.new_page()

        # 1) 注入登录态：app.html 启动时读 localStorage 自动 enterApp
        await page.goto(f"{BASE}/ui/app.html")
        await page.evaluate(
            "([t, u]) => { localStorage.setItem('pi_token', t); "
            "localStorage.setItem('pi_username', u); }",
            [token, username],
        )
        await page.reload()
        await page.wait_for_selector("#sessionList .sess", timeout=20000)
        await page.wait_for_timeout(800)
        await page.screenshot(path=f"{OUT}/01_app_sessions.png")
        print("SAVED", f"{OUT}/01_app_sessions.png")

        # 2) 点开会话，加载对话内容（含沙箱工具调用结果）
        await page.click("#sessionList .sess")
        await page.wait_for_timeout(2500)
        await page.screenshot(path=f"{OUT}/02_app_chat.png", full_page=True)
        print("SAVED", f"{OUT}/02_app_chat.png")

        # 3) 轨迹视图（时间线）—— 页面不会自动加载，需手动触发 load()
        tp = await ctx.new_page()
        await tp.goto(f"{BASE}/ui/trajectory.html?session={sid}")
        await tp.evaluate("load()")
        await tp.wait_for_timeout(2500)
        await tp.screenshot(path=f"{OUT}/03_trajectory.png", full_page=True)
        print("SAVED", f"{OUT}/03_trajectory.png")

        # 4) 知识库面板（点 📚 打开）
        await page.click("#openRag")
        await page.wait_for_timeout(1200)
        await page.screenshot(path=f"{OUT}/04_rag_panel.png")
        print("SAVED", f"{OUT}/04_rag_panel.png")

        await browser.close()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
