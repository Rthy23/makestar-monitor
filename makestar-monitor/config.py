"""
config.py — 平台切換中心

若要監控不同活動或平台，只需改這個檔案。
monitor.py / processor.py / app.py 不需要任何修改。
"""

import os
from datetime import datetime

# ── 活動設定 ─────────────────────────────────────────────────────────────────
GOODS_ID     = 29860
PRODUCT_NAME = "Yetimall 商品監控"
DEADLINE     = datetime(2026, 3, 16, 0, 0, 0)
FAST_START   = datetime(2026, 3, 15, 21, 0, 0)   # 此時間後切換 5 秒高頻模式

# ── 目標網址（Playwright 開啟的頁面）────────────────────────────────────────
PRODUCT_URL = f"https://m.yetimall.store/h5/#/goods?gid={GOODS_ID}"

# ── 訂單 API 攔截關鍵字（URL 中包含其中一個即觸發解析）────────────────────────
# 若 yetimall 的結帳 API 路徑不同，在此追加對應關鍵字即可。
ORDER_URL_KEYWORDS: tuple[str, ...] = (
    "payment",
    "order",
    "checkout",
    "api/v1/order",
    "submit",
    "purchase",
)

# ── 採樣間隔（秒）────────────────────────────────────────────────────────────
NORMAL_INTERVAL  = 30    # 平時
FAST_INTERVAL    = 5     # FAST_START 後
SILENT_INTERVAL  = 60    # 429 靜默模式
JITTER_LOW       = 1.0   # 隨機抖動下限（秒）
JITTER_HIGH      = 3.0   # 隨機抖動上限（秒）

# ── 資料庫路徑 ────────────────────────────────────────────────────────────────
DB_PATH = os.path.join(os.path.dirname(__file__), "monitor.db")

# ── Playwright keep-alive 週期（毫秒）────────────────────────────────────────
KEEPALIVE_INTERVAL_MS = 15_000
