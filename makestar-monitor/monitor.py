"""
monitor.py — Playwright API 監聽器

策略：
  - 攔截所有發往 yetimall.store 的 fetch / XHR 請求
  - 二次過濾：JSON body 含 ORDER_URL_KEYWORDS 才嘗試解析訂單
  - 解析結果交給 processor.record_order() UPSERT 到 DB
  - Session 崩潰後自動重啟，直到 DEADLINE

平台切換：只改 config.py，本檔不需動。
"""

import asyncio
import glob
import json
import logging
import random
import shutil
import time
from datetime import datetime

import processor
from config import (
    GOODS_ID, PRODUCT_URL, DEADLINE,
    ORDER_URL_KEYWORDS, KEEPALIVE_INTERVAL_MS,
    JITTER_LOW, JITTER_HIGH,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)

# 目標域名：只處理發往這裡的請求
_TARGET_HOST = "yetimall.store"

# ---------------------------------------------------------------------------
# Chromium finder
# ---------------------------------------------------------------------------

def _find_chromium() -> str | None:
    for name in ("chromium", "chromium-browser", "google-chrome"):
        p = shutil.which(name)
        if p:
            return p
    paths = glob.glob("/nix/store/*/bin/chromium")
    return paths[0] if paths else None

# ---------------------------------------------------------------------------
# Playwright session
# ---------------------------------------------------------------------------

async def _run_session(seen: set) -> None:
    from playwright.async_api import async_playwright

    chromium_path = _find_chromium()

    async with async_playwright() as p:
        launch_kwargs: dict = {
            "headless": True,
            "args": [
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-dev-shm-usage",
            ],
        }
        if chromium_path:
            launch_kwargs["executable_path"] = chromium_path

        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Linux; Android 13; SM-G991B) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Mobile Safari/537.36"
            ),
            locale="zh-CN",
            viewport={"width": 390, "height": 844},
        )
        page = await context.new_page()

        # ── requestfinished 事件處理器 ────────────────────────────────────
        async def on_request_finished(request):
            try:
                url = request.url

                # 第一層過濾：必須是發往目標域名的請求
                if _TARGET_HOST not in url:
                    return

                response = await request.response()
                if response is None or response.status != 200:
                    return

                ct = response.headers.get("content-type", "")
                if "json" not in ct:
                    return

                body = await response.body()
                body_str = body.decode("utf-8", errors="ignore")

                # 第二層過濾：URL 或 body 含訂單關鍵字才解析
                url_hit  = any(kw in url      for kw in ORDER_URL_KEYWORDS)
                body_hit = any(kw in body_str for kw in (
                    "orderId", "order_id", "purchaseId", "transactionId",
                ))
                if not (url_hit or body_hit):
                    return

                try:
                    json_data = json.loads(body)
                except Exception:
                    return

                ts     = datetime.now().isoformat()
                orders = processor.extract_order_data(json_data, ts)

                if not orders:
                    logger.debug(f"[intercept] matched URL but no orders: {url}")
                    return

                new_count = sum(
                    1 for o in orders if processor.record_order(o, seen)
                )
                total = len(orders)
                logger.info(
                    f"[intercept] {new_count} new / {total} total order(s) "
                    f"from {url.split('?')[0]}"
                )

            except Exception as exc:
                logger.debug(f"[intercept] handler error: {exc}")

        page.on("requestfinished", on_request_finished)

        # ── 初始導航 ──────────────────────────────────────────────────────
        logger.info(f"Navigating → {PRODUCT_URL}")
        try:
            await page.goto(PRODUCT_URL, wait_until="networkidle", timeout=40_000)
        except Exception as exc:
            logger.warning(f"Navigation warning (non-fatal): {exc}")
        logger.info("Page loaded. Listening for API requests to yetimall.store…")

        # ── Keep-alive 迴圈 ───────────────────────────────────────────────
        while datetime.now() < DEADLINE:
            await page.wait_for_timeout(KEEPALIVE_INTERVAL_MS)
            if page.is_closed():
                logger.warning("Page closed unexpectedly — restarting session.")
                break

        await browser.close()

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run() -> None:
    processor.init_db()
    seen = processor.load_seen_orders()
    logger.info(
        f"Monitor started — goods {GOODS_ID} | deadline {DEADLINE} | "
        f"pre-loaded {len(seen)} known order(s) | target: {_TARGET_HOST}"
    )

    while datetime.now() < DEADLINE:
        try:
            asyncio.run(_run_session(seen))
        except Exception as exc:
            wait = random.uniform(JITTER_LOW * 3, JITTER_HIGH * 5)
            logger.error(f"Session error: {exc} — restarting in {wait:.1f}s")
            time.sleep(wait)

    logger.info("Deadline reached. Monitor stopped.")


if __name__ == "__main__":
    run()
