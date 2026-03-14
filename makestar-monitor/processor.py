"""
processor.py — 核心處理層

Schema:  transactions(id, order_id UNIQUE, user_id, quantity,
                      timestamp, country, metadata, is_automated)

UPSERT 策略：
  - 同一 order_id 再次出現 → 累加 quantity + 更新 country（有值才覆蓋）
  - 不同 order_id 同一 user → 多行，各自獨立
"""

import json
import logging
import sqlite3
from datetime import datetime

import cloud_db
from config import DB_PATH

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# JSON key 優先序
# ---------------------------------------------------------------------------

_ORDER_ID_KEYS = (
    "orderId", "order_id", "orderCode", "orderNo",
    "transactionId", "transaction_id", "purchaseId", "purchase_id",
)
_USER_ID_KEYS = (
    "userId", "user_id", "buyerId", "buyer_id",
    "participantId", "participant_id", "memberId", "member_id",
    "nickname", "userName", "username",
)
_QTY_KEYS = (
    "quantity", "qty", "count", "item_count", "itemCount",
    "purchaseQuantity", "purchase_quantity",
    "orderQuantity",   "order_quantity",
)

# 地區相關 key（直接值）
_COUNTRY_KEYS = (
    "country", "countryCode", "country_code",
    "region", "area", "nation",
    "deliveryCountry", "delivery_country",
    "shippingCountry", "shipping_country",
)

# 地址子物件 key（向下找一層）
_ADDRESS_KEYS = (
    "address", "shippingAddress", "shipping_address",
    "deliveryAddress", "delivery_address",
    "userAddress", "user_address",
    "recipientAddress", "recipient_address",
)

# user 資訊子物件 key
_USER_INFO_KEYS = (
    "userInfo", "user_info", "buyerInfo", "buyer_info",
    "memberInfo", "member_info",
)

# 備用 metadata key（IP / 時區）
_META_KEYS = (
    "ip", "ipAddress", "ip_address", "clientIp", "client_ip",
    "timezone", "timeZone", "time_zone",
    "locale", "lang", "language",
)

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

def init_db() -> None:
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    required = {"order_id", "user_id", "quantity", "timestamp"}
    existing = {row[1] for row in c.execute("PRAGMA table_info(transactions)")}

    if existing and not required.issubset(existing):
        logger.info("DB migration: rebuilding transactions table")
        c.execute("ALTER TABLE transactions RENAME TO transactions_old")
        _create_transactions(c)
        try:
            c.execute("""
                INSERT INTO transactions
                    (order_id, user_id, quantity, timestamp)
                SELECT order_id, user_id,
                       COALESCE(quantity, 1),
                       COALESCE(timestamp, datetime('now'))
                FROM   transactions_old
            """)
        except Exception:
            pass
        c.execute("DROP TABLE IF EXISTS transactions_old")
        logger.info("DB migration: transactions rebuilt")
    else:
        _create_transactions(c)

    # Safe-add new columns (idempotent)
    existing2 = {row[1] for row in c.execute("PRAGMA table_info(transactions)")}
    safe_adds = [
        ("is_automated", "INTEGER NOT NULL DEFAULT 0"),
        ("country",      "TEXT"),
        ("metadata",     "TEXT"),
    ]
    for col_name, col_def in safe_adds:
        if col_name not in existing2:
            c.execute(f"ALTER TABLE transactions ADD COLUMN {col_name} {col_def}")
            logger.info(f"DB migration: added transactions.{col_name}")

    conn.commit()
    conn.close()
    logger.info("Database ready.")


def _create_transactions(c: sqlite3.Cursor) -> None:
    c.execute("""
        CREATE TABLE IF NOT EXISTS transactions (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            order_id     TEXT UNIQUE,
            user_id      TEXT,
            quantity     INTEGER NOT NULL DEFAULT 1,
            timestamp    TEXT    NOT NULL,
            country      TEXT,
            metadata     TEXT,
            is_automated INTEGER NOT NULL DEFAULT 0
        )
    """)


def _connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_PATH)


def load_seen_orders() -> set:
    conn = _connect()
    c = conn.cursor()
    try:
        rows = c.execute(
            "SELECT order_id FROM transactions WHERE order_id IS NOT NULL"
        ).fetchall()
        return {r[0] for r in rows}
    finally:
        conn.close()

# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

def _probe(obj: dict, keys: tuple):
    for k in keys:
        v = obj.get(k)
        if v is not None and v != "" and v != 0:
            return v
    return None


def _extract_geo(data: dict) -> tuple[str | None, str | None]:
    """
    從單一 dict（淺層優先）提取 (country, metadata)。

    搜索順序：
      1. 頂層 country 欄位
      2. address / shippingAddress 子物件中的 country 欄位
      3. user_info 子物件中的 country 欄位
      4. 退而求其次：IP / timezone → 存入 metadata
    """
    country  = None
    metadata = None

    # 1. 頂層直接命中
    country = _probe(data, _COUNTRY_KEYS)

    # 2. 地址子物件
    if not country:
        for addr_key in _ADDRESS_KEYS:
            addr = data.get(addr_key)
            if isinstance(addr, dict):
                country = _probe(addr, _COUNTRY_KEYS)
                if country:
                    break

    # 3. 用戶資訊子物件
    if not country:
        for ui_key in _USER_INFO_KEYS:
            ui = data.get(ui_key)
            if isinstance(ui, dict):
                country = _probe(ui, _COUNTRY_KEYS)
                if country:
                    break

    # 4. 備用 metadata（IP / 時區）
    meta_dict = {}
    for mk in _META_KEYS:
        v = data.get(mk)
        if v and str(v).strip():
            meta_dict[mk] = str(v)
    if meta_dict:
        metadata = json.dumps(meta_dict, ensure_ascii=False)

    return (str(country).strip() if country else None), metadata


def _walk(data, results: list, depth: int = 0) -> None:
    """遞迴找含有 order_id + user_id 的 dict。"""
    if depth > 20:
        return
    if isinstance(data, dict):
        oid = _probe(data, _ORDER_ID_KEYS)
        uid = _probe(data, _USER_ID_KEYS)
        qty = _probe(data, _QTY_KEYS)
        if oid and uid:
            country, metadata = _extract_geo(data)
            results.append({
                "order_id": str(oid),
                "user_id":  str(uid),
                "quantity": int(qty) if isinstance(qty, (int, float)) else 1,
                "country":  country,
                "metadata": metadata,
            })
        for v in data.values():
            if isinstance(v, (dict, list)):
                _walk(v, results, depth + 1)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, (dict, list)):
                _walk(item, results, depth + 1)


def extract_order_data(json_data, timestamp: str) -> list[dict]:
    """
    從 API 回應 JSON 提取訂單記錄。

    Returns:
        list of {order_id, user_id, quantity, timestamp, country, metadata}
    """
    results: list[dict] = []
    _walk(json_data, results)
    for r in results:
        r["timestamp"] = timestamp
    return results

# ---------------------------------------------------------------------------
# DB 寫入（UPSERT）
# ---------------------------------------------------------------------------

def record_order(order: dict, seen: set) -> bool:
    """
    UPSERT 一筆訂單到 transactions。

    - 新 order_id → INSERT
    - 已存在 order_id → 累加 quantity + 更新 country（有新值才覆蓋）

    seen set 僅用於加速判斷「是否為全新訂單」（True = 首次寫入）。
    即使 order_id 在 seen 中，也會執行 UPSERT 以更新 country/quantity。
    """
    oid = order["order_id"]
    is_new = oid not in seen

    conn = _connect()
    c = conn.cursor()
    try:
        c.execute("""
            INSERT INTO transactions
                (order_id, user_id, quantity, timestamp, country, metadata)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(order_id) DO UPDATE SET
                quantity  = transactions.quantity + excluded.quantity,
                country   = COALESCE(excluded.country,  transactions.country),
                metadata  = COALESCE(excluded.metadata, transactions.metadata),
                timestamp = excluded.timestamp
        """, (
            oid,
            order["user_id"],
            order["quantity"],
            order["timestamp"],
            order.get("country"),
            order.get("metadata"),
        ))
        conn.commit()
        seen.add(oid)

        geo_str = f" [{order['country']}]" if order.get("country") else ""
        action  = "NEW " if is_new else "UPD "
        logger.info(
            f"[{action}] order_id={oid!r}  user_id={order['user_id']!r}  "
            f"qty={order['quantity']}{geo_str}  ts={order['timestamp']}"
        )
        if is_new:
            cloud_db.write_transaction(order["timestamp"], order["quantity"])
        return is_new

    except Exception as exc:
        logger.warning(f"[record_order] error for {oid!r}: {exc}")
        return False
    finally:
        conn.close()
