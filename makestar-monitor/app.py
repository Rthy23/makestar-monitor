"""
app.py — 電商活動競爭格局儀表板

核心視角：長期累計，而非短期活躍度
  ① 累計銷售 Top 10 排行榜（含地區）
  ② 門檻差距分析（你 vs 第一名）
  ③ Top 3 用戶累計增長折線圖
  ④ 銷售地區分布
  ⑤ 原始訂單流水（Tab）
"""

import sqlite3
from datetime import datetime, timedelta

import pandas as pd
import streamlit as st

from config import (
    GOODS_ID, PRODUCT_NAME, DEADLINE, FAST_START, DB_PATH,
)

AUTO_REFRESH_SECS = 15

# ── 你自己的 user_id（選填）─────────────────────────────────────────────────
# 填入後，「門檻差距分析」會顯示你目前的累計數量和追趕距離。
MY_USER_ID: str = ""

# ── 注水偵測門檻 ──────────────────────────────────────────────────────────────
BOT_INTERVAL_SECS = 2.0
BOT_CONSECUTIVE   = 3

# ---------------------------------------------------------------------------
# DB helper
# ---------------------------------------------------------------------------

def _conn():
    return sqlite3.connect(DB_PATH)

# ---------------------------------------------------------------------------
# Data loaders
# ---------------------------------------------------------------------------

def load_summary() -> dict:
    try:
        conn = _conn()
        c = conn.cursor()
        c.execute("SELECT COUNT(*), COALESCE(SUM(quantity), 0), MAX(timestamp) FROM transactions")
        total_orders, total_qty, updated_at = c.fetchone()
        conn.close()
        return {"total_orders": total_orders or 0,
                "total_qty":    total_qty    or 0,
                "updated_at":   updated_at}
    except Exception:
        return {"total_orders": 0, "total_qty": 0, "updated_at": None}


def load_top10() -> pd.DataFrame:
    """
    累計銷售 Top 10：
    SELECT user_id, country, SUM(quantity) FROM transactions
    GROUP BY user_id ORDER BY total_qty DESC LIMIT 10
    """
    try:
        conn = _conn()
        df = pd.read_sql_query("""
            SELECT
                user_id,
                COALESCE(MAX(country), '—')  AS country,
                SUM(quantity)                AS total_qty,
                COUNT(*)                     AS order_count,
                MIN(timestamp)               AS first_seen,
                MAX(timestamp)               AS last_seen,
                MAX(is_automated)            AS is_bot
            FROM   transactions
            WHERE  user_id IS NOT NULL AND user_id != ''
            GROUP  BY user_id
            ORDER  BY total_qty DESC, first_seen ASC
            LIMIT  10
        """, conn)
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()


def load_my_total() -> int:
    """若設置了 MY_USER_ID，查詢我的累計購買數量。"""
    if not MY_USER_ID.strip():
        return 0
    try:
        conn = _conn()
        c = conn.cursor()
        c.execute(
            "SELECT COALESCE(SUM(quantity), 0) FROM transactions WHERE user_id = ?",
            (MY_USER_ID.strip(),),
        )
        total = c.fetchone()[0]
        conn.close()
        return int(total)
    except Exception:
        return 0


def load_top3_growth() -> pd.DataFrame:
    """
    Top 3 用戶的每筆交易時間 + 累計數量（用於折線圖）。
    回傳 wide-format DataFrame，index=timestamp，columns=user_id_1..3
    """
    try:
        conn = _conn()
        top3 = pd.read_sql_query("""
            SELECT user_id, SUM(quantity) as total_qty
            FROM   transactions
            WHERE  user_id IS NOT NULL AND user_id != ''
            GROUP  BY user_id
            ORDER  BY total_qty DESC
            LIMIT  3
        """, conn)

        if top3.empty:
            conn.close()
            return pd.DataFrame()

        top3_ids = top3["user_id"].tolist()
        placeholders = ",".join("?" * len(top3_ids))
        raw = pd.read_sql_query(f"""
            SELECT user_id, timestamp, quantity
            FROM   transactions
            WHERE  user_id IN ({placeholders})
            ORDER  BY timestamp ASC
        """, conn, params=top3_ids)
        conn.close()

        if raw.empty:
            return pd.DataFrame()

        raw["timestamp"] = pd.to_datetime(raw["timestamp"], errors="coerce")
        raw = raw.dropna(subset=["timestamp"])

        # Cumulative qty per user
        series = {}
        for uid in top3_ids:
            user_df = raw[raw["user_id"] == uid].sort_values("timestamp")
            user_df = user_df.set_index("timestamp")["quantity"].cumsum()
            label = f"{uid[:12]}…" if len(uid) > 14 else uid
            series[label] = user_df

        combined = pd.DataFrame(series)
        combined = combined.sort_index().ffill()
        return combined

    except Exception:
        return pd.DataFrame()


def load_country_stats() -> pd.DataFrame:
    try:
        conn = _conn()
        df = pd.read_sql_query("""
            SELECT
                COALESCE(country, '未知地區') AS country,
                COUNT(*)                      AS order_count,
                SUM(quantity)                 AS total_qty
            FROM   transactions
            GROUP  BY COALESCE(country, '未知地區')
            ORDER  BY total_qty DESC
        """, conn)
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()


def load_transaction_stream(limit: int = 100) -> pd.DataFrame:
    try:
        conn = _conn()
        df = pd.read_sql_query(f"""
            SELECT timestamp, order_id, user_id, quantity, country, is_automated
            FROM   transactions
            ORDER  BY id DESC
            LIMIT  {limit}
        """, conn)
        conn.close()
        return df
    except Exception:
        return pd.DataFrame()

# ---------------------------------------------------------------------------
# Bot detection
# ---------------------------------------------------------------------------

def detect_bot_users(df: pd.DataFrame) -> set:
    bots: set = set()
    if df.empty or "user_id" not in df.columns:
        return bots
    tmp = df.copy()
    tmp["timestamp"] = pd.to_datetime(tmp["timestamp"], errors="coerce")
    tmp = tmp.dropna(subset=["timestamp", "user_id"])
    tmp = tmp[tmp["user_id"].astype(str).str.strip() != ""]
    for uid, grp in tmp.groupby("user_id"):
        times = grp.sort_values("timestamp")["timestamp"].tolist()
        if len(times) < BOT_CONSECUTIVE:
            continue
        intervals = [(times[i+1]-times[i]).total_seconds() for i in range(len(times)-1)]
        window = BOT_CONSECUTIVE - 1
        for i in range(len(intervals) - window + 1):
            if all(iv < BOT_INTERVAL_SECS for iv in intervals[i:i+window]):
                bots.add(str(uid))
                break
    return bots

# ---------------------------------------------------------------------------
# Rank badge
# ---------------------------------------------------------------------------

def _badge(rank: int, is_bot: bool) -> str:
    prefix = "⚠️ " if is_bot else ""
    icons = {1: "🥇", 2: "🥈", 3: "🥉"}
    return f"{prefix}{icons.get(rank, f'#{rank}')}"

# ---------------------------------------------------------------------------
# Main dashboard
# ---------------------------------------------------------------------------

def main():
    st.set_page_config(
        page_title=f"競爭格局監控 #{GOODS_ID}",
        page_icon="🏆",
        layout="wide",
    )

    now       = datetime.now()
    is_active = now < DEADLINE
    is_fast   = now >= FAST_START and is_active

    # ── Header ───────────────────────────────────────────────────────────────
    st.title("🏆 累計競爭格局分析儀表板")
    st.caption(
        f"商品 #{GOODS_ID} · {PRODUCT_NAME} · "
        f"截止 {DEADLINE.strftime('%Y-%m-%d %H:%M')}"
    )

    if not is_active:
        st.error("⏹️ 活動已結束，監控已停止。")
    else:
        rem = DEADLINE - now
        h, r = divmod(int(rem.total_seconds()), 3600)
        m, s = divmod(r, 60)
        mode = "⚡ 高頻模式 (5s)" if is_fast else "🕐 常規模式 (30s)"
        st.info(f"⏱️ 距截止：**{h}h {m}m {s}s** &nbsp;&nbsp;|&nbsp;&nbsp; {mode}")

    # ── Top metrics ──────────────────────────────────────────────────────────
    summary   = load_summary()
    top10_df  = load_top10()
    my_total  = load_my_total()

    first_qty = int(top10_df["total_qty"].iloc[0]) if not top10_df.empty else 0
    gap_to_1  = max(0, first_qty - my_total)

    m1, m2, m3, m4 = st.columns(4)
    with m1:
        st.metric("偵測到的訂單數", summary["total_orders"])
    with m2:
        st.metric("累計購買總張數", summary["total_qty"])
    with m3:
        st.metric("🥇 第一名累計", first_qty)
    with m4:
        if MY_USER_ID.strip():
            st.metric("我的累計", my_total, delta=f"-{gap_to_1}" if gap_to_1 > 0 else "領先！")
        else:
            st.metric("我的累計", "— (未設定)", help="在 app.py 設定 MY_USER_ID 後顯示")

    if summary["updated_at"]:
        st.caption(f"最後更新：{str(summary['updated_at'])[:19]}")

    st.markdown("---")

    # ════════════════════════════════════════════════════════════════════════
    # 門檻差距分析
    # ════════════════════════════════════════════════════════════════════════
    if not top10_df.empty:
        first_row = top10_df.iloc[0]
        first_uid = str(first_row["user_id"])[:20]

        if MY_USER_ID.strip():
            if MY_USER_ID.strip() == top10_df.iloc[0]["user_id"]:
                st.success(
                    f"🥇 **你就是第一名！** 累計購買 **{my_total}** 張，"
                    f"領先第二名 **{first_qty - (int(top10_df.iloc[1]['total_qty']) if len(top10_df) > 1 else 0)}** 張。"
                )
            elif my_total > 0:
                st.warning(
                    f"**第一名**（`{first_uid}…`）累計：**{first_qty}** 張　｜　"
                    f"**你的累計**：**{my_total}** 張　｜　"
                    f"🎯 距離第一還差：**{gap_to_1}** 張"
                )
            else:
                st.info(
                    f"**第一名**（`{first_uid}…`）累計：**{first_qty}** 張　｜　"
                    f"你的 user_id `{MY_USER_ID}` 尚未在資料庫中出現。"
                )
        else:
            st.info(
                f"🥇 **第一名**（`{first_uid}…`）目前累計：**{first_qty}** 張　"
                f"｜ 在 `app.py` 設定 `MY_USER_ID` 可顯示你的追趕差距。"
            )

    st.markdown("---")

    # ════════════════════════════════════════════════════════════════════════
    # 累計銷售 Top 10 排行榜
    # ════════════════════════════════════════════════════════════════════════
    st.subheader("🏆 累計銷售排行榜 Top 10")

    if top10_df.empty:
        st.info(
            "排行榜尚無資料。\n\n"
            "監控器正在監聽 `yetimall.store` 的 API 回應，"
            "含 `userId` + `orderId` 的 JSON 出現後自動填入。"
        )
    else:
        # Bot detection on full txn table for flagging
        try:
            txn_full = pd.read_sql_query(
                "SELECT user_id, timestamp FROM transactions", _conn()
            )
            bot_ids = detect_bot_users(txn_full)
        except Exception:
            bot_ids = set()

        rows = []
        for rank, (_, r) in enumerate(top10_df.iterrows(), start=1):
            uid      = str(r["user_id"])
            is_bot   = uid in bot_ids
            is_me    = MY_USER_ID.strip() and uid == MY_USER_ID.strip()
            badge    = _badge(rank, is_bot)
            note     = " 👤 我" if is_me else ("⚠️ Bot" if is_bot else "")
            rows.append({
                "排名":     badge,
                "用戶 ID":  uid,
                "地區":     str(r["country"]),
                "累計購買張數": int(r["total_qty"]),
                "訂單筆數": int(r["order_count"]),
                "首次出現": str(r["first_seen"])[:16],
                "最後出現": str(r["last_seen"])[:16],
                "備注":     note,
            })

        display_df = pd.DataFrame(rows)

        def _highlight(row):
            note = str(row.get("備注", ""))
            if "我" in note:
                return ["background-color: #d4edda"] * len(row)
            if "Bot" in note:
                return ["background-color: #fff3cd"] * len(row)
            if row.get("排名", "").startswith("🥇"):
                return ["background-color: #fef9e7"] * len(row)
            return [""] * len(row)

        st.dataframe(
            display_df.style.apply(_highlight, axis=1),
            use_container_width=True,
            hide_index=True,
        )
        st.markdown(
            "<small>🥇🥈🥉 前三名 &nbsp;|&nbsp; 👤 我 &nbsp;|&nbsp; ⚠️ 疑似 Bot</small>",
            unsafe_allow_html=True,
        )

    st.markdown("---")

    # ════════════════════════════════════════════════════════════════════════
    # Top 3 用戶累計增長折線圖
    # ════════════════════════════════════════════════════════════════════════
    st.subheader("📈 Top 3 用戶累計增長曲線")

    growth_df = load_top3_growth()

    if growth_df.empty:
        st.info("累計增長圖需要至少 1 筆交易資料後才會顯示。")
    else:
        st.markdown(
            "折線代表 **Top 3 用戶各自的累計購買張數**，"
            "斜率越陡代表購買節奏越快。"
        )
        st.line_chart(growth_df, use_container_width=True)

    st.markdown("---")

    # ════════════════════════════════════════════════════════════════════════
    # 銷售地區分布
    # ════════════════════════════════════════════════════════════════════════
    st.subheader("🌏 銷售地區分布")
    country_df = load_country_stats()

    if country_df.empty:
        st.info("尚無地區資料。當 API 回應含有 `country` / `country_code` 欄位時將自動填入。")
    else:
        col_chart, col_table = st.columns([2, 1])
        with col_chart:
            st.bar_chart(
                country_df.set_index("country")[["total_qty"]],
                use_container_width=True,
            )
        with col_table:
            pct = country_df["total_qty"] / country_df["total_qty"].sum() * 100
            geo = country_df.copy()
            geo["占比"] = pct.map(lambda x: f"{x:.1f}%")
            geo = geo.rename(columns={
                "country":     "地區",
                "order_count": "訂單數",
                "total_qty":   "購買張數",
            })
            st.dataframe(geo, use_container_width=True, hide_index=True)

    st.markdown("---")

    # ── Tabs ─────────────────────────────────────────────────────────────────
    tab1, tab2 = st.tabs(["🔍 原始訂單流", "⚙️ 設定說明"])

    with tab1:
        st.markdown("#### 最新 100 筆訂單流水")
        txn_df = load_transaction_stream(100)
        if txn_df.empty:
            st.warning(
                "transactions 表為空。\n\n"
                "監控器正在監聽 `yetimall.store` 的 API 回應，"
                "含 `orderId` + `userId` 的 JSON 出現後自動記錄。\n\n"
                "若訂單 API 需要登入 Session，此表將維持空白——這是平台限制。"
            )
        else:
            dt = txn_df.copy()
            dt["timestamp"]    = dt["timestamp"].astype(str).str[:19]
            dt["is_automated"] = dt["is_automated"].map({1: "⚠️ Bot", 0: "✅"}).fillna("✅")
            dt = dt.rename(columns={
                "timestamp":    "時間",
                "order_id":     "訂單 ID",
                "user_id":      "用戶 ID",
                "quantity":     "數量",
                "country":      "地區",
                "is_automated": "狀態",
            })
            st.dataframe(dt, use_container_width=True, hide_index=True)

    with tab2:
        st.markdown("### ⚙️ 設定說明")
        st.markdown(f"""
| 參數 | 目前值 | 說明 | 位置 |
|------|--------|------|------|
| `GOODS_ID` | `{GOODS_ID}` | 目標商品 ID | `config.py` |
| `PRODUCT_URL` | yetimall.store | 監控目標 URL | `config.py` |
| `MY_USER_ID` | `"{MY_USER_ID or '未設定'}"` | 你的用戶 ID | `app.py` 頂部 |
| `BOT_INTERVAL_SECS` | `{BOT_INTERVAL_SECS}` | Bot 偵測間隔閾值（秒）| `app.py` 頂部 |
| `AUTO_REFRESH_SECS` | `{AUTO_REFRESH_SECS}` | 頁面自動刷新（秒）| `app.py` 頂部 |

**模組職責：**
| 檔案 | 職責 |
|------|------|
| `config.py` | 平台切換（URL / 關鍵字 / 時間）|
| `processor.py` | JSON 解析 + DB 寫入 |
| `monitor.py` | Playwright 監聽器 |
| `app.py` | 競爭格局儀表板（只讀 DB）|
        """)

    # ── Auto-refresh ─────────────────────────────────────────────────────────
    st.markdown(
        f"<meta http-equiv='refresh' content='{AUTO_REFRESH_SECS}'>",
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
