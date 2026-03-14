"""
browser_fetcher.py

Three-strategy fetch pipeline:
1. Direct JSON API  (fastest, ~0.2s) — primary
2. SSR HTML parse  (fast, ~1-2s)    — fallback
3. Playwright      (slow, ~10s)     — last resort

Returns HTTP status code so callers can handle 429 / rate-limit.
Each call uses a freshly-rotated User-Agent to mimic organic traffic.
"""

import asyncio
import json
import logging
import os
import random
import re
import shutil
from datetime import datetime

import requests

logger = logging.getLogger(__name__)

PRODUCT_URL_TEMPLATE = "https://www.makestar.com/product/{campaign_id}"
DYNAMIC_API_TEMPLATE = (
    "https://new-commerce-api.makestar.com/v2/commerce/product_event/{campaign_id}/dynamic/"
)

# Pool of realistic desktop/mobile UAs to rotate through
_UA_POOL = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.4 Mobile/15E148 Safari/604.1",
    "Mozilla/5.0 (Linux; Android 14; SM-S918B) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Mobile Safari/537.36",
    "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
]


def _random_ua() -> str:
    return random.choice(_UA_POOL)


def _make_session(ua: str | None = None) -> requests.Session:
    ua = ua or _random_ua()
    s = requests.Session()
    s.headers.update({
        "User-Agent": ua,
        "Accept": "application/json, text/html, */*;q=0.8",
        "Accept-Language": "ko-KR,ko;q=0.9,en-US;q=0.8,en;q=0.7",
        "Referer": "https://www.makestar.com/",
        "Origin": "https://www.makestar.com",
        "Cache-Control": "no-cache",
    })
    return s


def _find_chromium_executable() -> str | None:
    for name in ("chromium", "chromium-browser", "google-chrome"):
        p = shutil.which(name)
        if p:
            return p
    import glob
    paths = glob.glob("/nix/store/*/bin/chromium")
    return paths[0] if paths else None


# ---------------------------------------------------------------------------
# Strategy 1: Direct JSON API
# ---------------------------------------------------------------------------

def fetch_dynamic_api(campaign_id: int) -> dict:
    """
    Returns:
        {stock, isPurchasable, saleStatus, isDisplayStock, source_url, http_status}
    http_status is the raw HTTP status code (e.g. 200, 429, 500).
    """
    url = DYNAMIC_API_TEMPLATE.format(campaign_id=campaign_id)
    ua = _random_ua()
    session = _make_session(ua)
    try:
        resp = session.get(url, timeout=10)
        status = resp.status_code

        if status == 429:
            logger.warning(f"[direct-api] 429 Too Many Requests")
            return {"stock": None, "isPurchasable": None, "saleStatus": None,
                    "source_url": url, "http_status": 429}

        resp.raise_for_status()
        body = resp.json()
        data = body.get("data", {})
        if not data:
            return {"stock": None, "isPurchasable": None, "saleStatus": None,
                    "source_url": url, "http_status": status}

        stock = data.get("stock")
        if isinstance(stock, int) and stock < 0:
            stock = None

        return {
            "stock": stock if isinstance(stock, int) else None,
            "isPurchasable": data.get("isPurchasable"),
            "saleStatus": data.get("saleStatus"),
            "isDisplayStock": data.get("isDisplayStock"),
            "displayStatus": data.get("displayStatus"),
            "source_url": url,
            "http_status": status,
        }
    except requests.HTTPError as e:
        code = e.response.status_code if e.response is not None else 0
        logger.warning(f"[direct-api] HTTP error {code}: {e}")
        return {"stock": None, "isPurchasable": None, "saleStatus": None,
                "source_url": url, "http_status": code}
    except Exception as e:
        logger.warning(f"[direct-api] fetch failed: {e}")
        return {"stock": None, "isPurchasable": None, "saleStatus": None,
                "source_url": url, "http_status": 0}


# ---------------------------------------------------------------------------
# Strategy 2: SSR HTML parsing (Nuxt revive payload)
# ---------------------------------------------------------------------------

def _resolve_nuxt_payload(payload: list) -> dict:
    if not isinstance(payload, list) or not payload:
        return {}

    def resolve(val, depth=0):
        if depth > 25:
            return val
        if isinstance(val, list) and len(val) == 2 and val[0] in ("ShallowReactive", "Reactive"):
            idx = val[1]
            if isinstance(idx, int) and 0 <= idx < len(payload):
                return resolve(payload[idx], depth + 1)
        if isinstance(val, dict):
            return {k: resolve_ref(v, depth + 1) for k, v in val.items()}
        if isinstance(val, list):
            return [resolve(item, depth + 1) for item in val]
        return val

    def resolve_ref(val, depth=0):
        if depth > 25:
            return val
        if isinstance(val, int) and 0 <= val < len(payload):
            item = payload[val]
            if isinstance(item, list) and len(item) == 2 and item[0] in ("ShallowReactive", "Reactive"):
                return resolve(item, depth + 1)
            if isinstance(item, (bool, str, float, type(None))):
                return item
            if isinstance(item, dict):
                return {k: resolve_ref(v, depth + 1) for k, v in item.items()}
            if isinstance(item, list):
                return [resolve_ref(i, depth + 1) for i in item]
        return resolve(val, depth)

    if (isinstance(payload[0], list) and len(payload[0]) == 2
            and payload[0][0] == "ShallowReactive"):
        root_idx = payload[0][1]
        if isinstance(root_idx, int) and 0 <= root_idx < len(payload):
            return resolve(payload[root_idx])
    return {}


def _extract_sale_info(data) -> dict:
    result = {"stock": None, "isPurchasable": None, "saleStatus": None}
    STOCK_KEYS = (
        "stock", "stockCount", "remainingStock", "remainingCount",
        "quantity", "availableCount", "availableQuantity",
        "purchasableCount", "stockAmount",
    )

    def walk(obj, depth=0):
        if depth > 20:
            return
        if isinstance(obj, dict):
            if result["saleStatus"] is None:
                v = obj.get("saleStatus")
                if isinstance(v, str) and v:
                    result["saleStatus"] = v
            if result["isPurchasable"] is None:
                v = obj.get("isPurchasable")
                if isinstance(v, bool):
                    result["isPurchasable"] = v
            if result["stock"] is None:
                for key in STOCK_KEYS:
                    v = obj.get(key)
                    if isinstance(v, (int, float)) and not isinstance(v, bool) and v >= 0:
                        result["stock"] = int(v)
                        break
            for v in obj.values():
                if isinstance(v, (dict, list)):
                    walk(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, (dict, list)):
                    walk(item, depth + 1)

    walk(data)
    return result


def _parse_ssr_html(html: str) -> dict:
    blocks = re.findall(
        r'<script[^>]*type=["\']application/json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL,
    )
    for raw in blocks:
        raw = raw.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, list):
            continue
        decoded = _resolve_nuxt_payload(payload)
        if decoded:
            info = _extract_sale_info(decoded)
            if info.get("saleStatus") or info.get("isPurchasable") is not None:
                return info
        info = _extract_sale_info(payload)
        if info.get("saleStatus"):
            return info
    return {"stock": None, "isPurchasable": None, "saleStatus": None}


def fetch_html_requests(campaign_id: int) -> tuple[str | None, str | None, int]:
    url = PRODUCT_URL_TEMPLATE.format(campaign_id=campaign_id)
    session = _make_session()
    try:
        resp = session.get(url, timeout=15, allow_redirects=True)
        return resp.text, resp.url, resp.status_code
    except requests.RequestException as e:
        logger.warning(f"[ssr] fetch failed: {e}")
        return None, None, 0


# ---------------------------------------------------------------------------
# Strategy 3: Playwright (headless, fallback)
# ---------------------------------------------------------------------------

_SCREENSHOT_DIR = os.path.join(os.path.dirname(__file__), "..", "screenshots")

# Keywords that identify sale-state API responses
_SALE_URL_KEYWORDS = (
    "product_event", "product", "signing", "sale", "stock", "dynamic",
)

# Keywords that identify order/purchase API responses to parse for orderId / userId
_ORDER_URL_KEYWORDS = (
    "order", "purchase", "checkout", "payment", "cart", "buy",
)

# JSON keys to probe for order ID (first match wins)
_ORDER_ID_KEYS = (
    "orderId", "order_id", "orderCode", "orderNo", "id",
)

# JSON keys to probe for user / participant identity
_USER_ID_KEYS = (
    "userId", "user_id", "buyerId", "buyer_id",
    "participantId", "participant_id",
    "nickname", "userName", "username",
    "memberId", "member_id",
)

# JSON keys to probe for quantity
_QTY_KEYS = (
    "quantity", "qty", "count", "amount",
    "purchaseQuantity", "purchase_quantity",
    "orderQuantity", "order_quantity",
    "itemCount", "item_count",
)


def _probe(obj: dict, keys: tuple) -> str | int | None:
    """Return first non-empty value found at any of *keys* in *obj* (shallow)."""
    for k in keys:
        v = obj.get(k)
        if v is not None and v != "" and v != 0:
            return v
    return None


def _extract_orders(data) -> list[dict]:
    """
    Recursively walk *data* looking for dicts that contain both an
    order-id key and a user-id key. Returns a list of
    {order_id, user_id, quantity} dicts.
    """
    found: list[dict] = []

    def walk(obj, depth=0):
        if depth > 20:
            return
        if isinstance(obj, dict):
            oid = _probe(obj, _ORDER_ID_KEYS)
            uid = _probe(obj, _USER_ID_KEYS)
            qty = _probe(obj, _QTY_KEYS)
            if oid and uid:
                found.append({
                    "order_id": str(oid),
                    "user_id":  str(uid),
                    "quantity": int(qty) if isinstance(qty, (int, float)) else 1,
                })
            for v in obj.values():
                if isinstance(v, (dict, list)):
                    walk(v, depth + 1)
        elif isinstance(obj, list):
            for item in obj:
                if isinstance(item, (dict, list)):
                    walk(item, depth + 1)

    walk(data)
    return found


async def _playwright_fetch(campaign_id: int) -> dict:
    from playwright.async_api import async_playwright
    url = PRODUCT_URL_TEMPLATE.format(campaign_id=campaign_id)
    chromium_path = _find_chromium_executable()

    sale_captured:  list[dict] = []   # for isPurchasable / saleStatus
    order_captured: list[dict] = []   # for order stream parsing

    async with async_playwright() as p:
        launch_kwargs = {
            "headless": True,
            "args": ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage"],
        }
        if chromium_path:
            launch_kwargs["executable_path"] = chromium_path

        browser = await p.chromium.launch(**launch_kwargs)
        context = await browser.new_context(
            user_agent=_random_ua(),
            locale="zh-TW",
        )
        page = await context.new_page()

        async def on_response(response):
            try:
                ct = response.headers.get("content-type", "")
                if response.status != 200 or "json" not in ct:
                    return
                resp_url = response.url
                body_bytes = await response.body()
                try:
                    data = json.loads(body_bytes)
                except Exception:
                    return

                # Sale-state interception
                if any(kw in resp_url for kw in _SALE_URL_KEYWORDS):
                    sale_captured.append({"url": resp_url, "data": data})

                # Order-stream interception: URL or body contains order keywords
                url_is_order = any(kw in resp_url for kw in _ORDER_URL_KEYWORDS)
                body_str     = body_bytes.decode("utf-8", errors="ignore")
                body_is_order = any(kw in body_str for kw in ("orderId", "order_id", "purchaseId"))

                if url_is_order or body_is_order:
                    orders = _extract_orders(data)
                    if orders:
                        logger.info(
                            f"[order-stream] {len(orders)} order(s) in {resp_url}"
                        )
                        order_captured.extend(orders)
                    else:
                        # Log raw body snippet for diagnostics when URL matched but parsing failed
                        if url_is_order:
                            logger.debug(
                                f"[order-stream] URL matched but no orders parsed: "
                                f"{resp_url} | body[:200]={body_str[:200]}"
                            )
            except Exception:
                pass

        page.on("response", on_response)
        await page.goto(url, wait_until="networkidle", timeout=35000)
        await page.wait_for_timeout(3000)
        html      = await page.content()
        final_url = page.url

        # Screenshot on first poll (always) for manual verification
        try:
            os.makedirs(_SCREENSHOT_DIR, exist_ok=True)
            # Only keep latest screenshot — overwrite same filename
            shot_path = os.path.join(_SCREENSHOT_DIR, "latest.png")
            await page.screenshot(path=shot_path, full_page=False)
        except Exception:
            pass

        await browser.close()

    # ── Build result ──────────────────────────────────────────────────────────
    result: dict = {
        "stock":         None,
        "isPurchasable": None,
        "saleStatus":    None,
        "source_url":    final_url,
        "http_status":   200,
        "orders":        order_captured,   # list of {order_id, user_id, quantity}
    }

    for cap in sale_captured:
        info = _extract_sale_info(cap["data"])
        if info.get("saleStatus") or info.get("isPurchasable") is not None:
            result.update(info)
            break

    if not result.get("saleStatus"):
        parsed = _parse_ssr_html(html)
        result.update({k: v for k, v in parsed.items() if v is not None})

    return result


def fetch_playwright(campaign_id: int) -> dict:
    return asyncio.run(_playwright_fetch(campaign_id))


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def fetch_sale_info(campaign_id: int, use_playwright: bool = False) -> dict:
    """
    Returns:
        stock, isPurchasable, saleStatus, source_url, http_status
    """
    if not use_playwright:
        info = fetch_dynamic_api(campaign_id)
        if info.get("http_status") == 429:
            return info
        if info.get("saleStatus") or info.get("isPurchasable") is not None:
            return info

        logger.info("[fetch] Direct API failed, trying SSR...")
        html, url, status = fetch_html_requests(campaign_id)
        if status == 429:
            return {"stock": None, "isPurchasable": None, "saleStatus": None,
                    "source_url": url, "http_status": 429}
        if html:
            info = _parse_ssr_html(html)
            info["source_url"] = url
            info["http_status"] = status
            if info.get("saleStatus") or info.get("isPurchasable") is not None:
                return info

        logger.info("[fetch] SSR failed, falling back to Playwright...")

    result = fetch_playwright(campaign_id)
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
    import sys
    cid = int(sys.argv[1]) if len(sys.argv) > 1 else 16183
    print(f"\n=== Campaign {cid} ===")
    r = fetch_sale_info(cid)
    print(json.dumps(r, indent=2, ensure_ascii=False))
