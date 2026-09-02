# -*- coding: utf-8 -*-
"""
HTML → PNG 渲染：基于 Playwright（对齐 KiraAI_daily_report_plugin 的浏览器策略）。

浏览器检测顺序（自动）：
  1. 系统默认浏览器（Chrome → Edge → Chromium，channel 启动）
  2. 都没有 → 自动下载 Playwright 内置 Chromium（python -m playwright install chromium）
  3. 下载/启动仍失败 → 渲染图模式降级不可用，插件其余功能不受影响
"""
from __future__ import annotations

import asyncio
import sys
from typing import Optional

from core.plugin import logger

# 优先尝试的系统浏览器 channel（与日报插件一致）
_BROWSER_CANDIDATES = (
    ("chrome", "Google Chrome"),
    ("msedge", "Microsoft Edge"),
    ("chromium", "Chromium"),
)

SCREENSHOT_WIDTH = 1200


async def _install_chromium() -> bool:
    """下载并安装 Playwright 内置 Chromium，返回是否成功。"""
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "playwright", "install", "chromium",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise RuntimeError(stderr.decode(errors="replace") if stderr else f"退出码 {proc.returncode}")
        logger.info("[token_stats] Chromium 下载完成")
    except Exception as e:
        logger.warning(f"[token_stats] Chromium 自动下载失败: {e}")
        return False
    # 验证安装
    try:
        from playwright.async_api import async_playwright
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            await browser.close()
        logger.info("[token_stats] Chromium 安装验证通过")
        return True
    except Exception as e:
        logger.warning(f"[token_stats] Chromium 安装后仍无法启动: {e}")
        return False


class BrowserManager:
    """浏览器环境：默认浏览器 → 内置 Chromium（自动下载）三级降级。"""

    def __init__(self):
        self._channel: Optional[str] = None
        self._ready = asyncio.Event()
        self._ok = False

    @property
    def channel(self) -> Optional[str]:
        return self._channel

    @property
    def ready(self) -> asyncio.Event:
        return self._ready

    @property
    def ok(self) -> bool:
        return self._ok

    async def initialize(self):
        try:
            from playwright.async_api import async_playwright
        except Exception as e:
            logger.warning(f"[token_stats] playwright 未安装，渲染图模式不可用: {e}")
            self._ready.set()
            return
        try:
            # 1) 系统默认浏览器
            for channel, display in _BROWSER_CANDIDATES:
                try:
                    async with async_playwright() as p:
                        browser = await p.chromium.launch(channel=channel, headless=True)
                        await browser.close()
                    logger.info(f"[token_stats] 检测到系统浏览器: {display}")
                    self._channel = channel
                    self._ok = True
                    return
                except Exception:
                    continue
            # 2) 内置 Chromium（若已安装）
            try:
                async with async_playwright() as p:
                    browser = await p.chromium.launch(headless=True)
                    await browser.close()
                self._ok = True
                logger.info("[token_stats] 使用 Playwright 内置 Chromium")
                return
            except Exception:
                # 3) 自动下载依赖
                logger.info("[token_stats] 未检测到可用浏览器，自动下载内置 Chromium...")
                if await _install_chromium():
                    self._ok = True
        except Exception as e:
            logger.warning(f"[token_stats] 浏览器初始化失败，渲染图模式不可用: {e}")
        finally:
            self._ready.set()


async def render_html(html: str, output_path: str, browser: BrowserManager, wait_js: str = None) -> str:
    """渲染 HTML → PNG 整页截图，返回输出路径。失败抛异常（由调用方降级文本）。
    wait_js：可选 JS 表达式，非空时等待其返回真值（如背景图加载完成），超时 8s 忽略。
    截图策略：先按内容实际高度动态设置视口再截图（flex/min-height 布局下 full_page
    的 scrollHeight 计算不可靠会截断），内容超高时自动撑高视口。"""
    if browser is not None:
        await browser.ready.wait()
        if not browser.ok:
            raise RuntimeError("浏览器未就绪（playwright 缺失或浏览器初始化失败）")
    from playwright.async_api import async_playwright

    launch_kwargs = {"headless": True}
    if browser is not None and browser.channel:
        launch_kwargs["channel"] = browser.channel

    async with async_playwright() as p:
        b = await p.chromium.launch(**launch_kwargs)
        try:
            page = await b.new_page(viewport={"width": SCREENSHOT_WIDTH, "height": 800})
            await page.set_content(html, wait_until="domcontentloaded", timeout=10000)
            if wait_js:
                try:
                    await page.wait_for_function(wait_js, timeout=8000)
                except Exception:
                    pass  # 超时忽略，继续截图
            await page.wait_for_timeout(800)
            # 按内容实际高度动态设置视口（flex 布局下 full_page scrollHeight 不可靠）
            try:
                h = await page.evaluate("Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)")
                h = max(800, int(h) + 2)
                await page.set_viewport_size({"width": SCREENSHOT_WIDTH, "height": h})
                await page.wait_for_timeout(200)
                await page.screenshot(path=output_path, full_page=False)
            except Exception:
                try:
                    await page.screenshot(path=output_path, full_page=True)
                except Exception:
                    await page.screenshot(path=output_path, full_page=False)
        finally:
            await b.close()
    return output_path
