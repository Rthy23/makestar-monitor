"""
app.py — 電商活動競爭格局儀表板

資料來源優先序：
  1. 本地 SQLite (monitor.db) — 有時使用（本機 / Replit 環境）
  2. Supabase REST API        — 找不到本地 DB 時自動切換（Streamlit Cloud）

核心報表：
  ① 累計銷售 Top 10 排行榜（含地區）
  ② 門檻差距分析（你 vs 第一名）
  ③ Top 3 用戶累計增長折線圖
  ④ 銷售地區分布
  ⑤ 原始訂單流水（Tab）
"""

import os
import sqlite3
from datetime import datetime

import pandas as pd
import streamlit as st

import cloud_db
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
# 統一資料層：SQLite ↔ Supabase 自動切換
# ---------------------------------------------------------------------------

def _use_local() -> bool:
    """本地 SQLite 檔案存在時用 SQLite，否則用 Supabase。"""
    return os.path.exists(DB_PATH)


@st.cache_data(ttl=AUTO_REFRESH_SECS)
def _load_raw_txns() -> pd.DataFrame:
    """
    回傳完整 transactions DataFrame，欄位：
    user_id, order_id, country, quantity, timestamp, is_automated
    本地優先，Streamlit Cloud 自動切 Supabase。
    """
    if _use_local():
        try:
            conn = sqlite3.connect(DB_PATH)
            df = pd.read_sql_query(
                "SELECT user_id, order_id, country, quantity, timestamp, is_automated "
                "FROM transactions ORDER BY id ASC",
                conn,
            )
            conn.close()
            return df
        except Exception:
            return pd.DataFrame()
    else:
        df = cloud_db.read_transactions_df()
        if df is None:
            return pd.DataFrame()
        return df


def _ensure_cols(df: pd.DataFrame) -> pd.DataFrame:
    """補齊缺欄以防後續運算出錯。"""
    defaults = {
        "user_id": None, "order_id": None, "country": None,
        "quantity": 1, "timestamp": None, "is_automated": 0,
    }
    for col, val in defaults.items():
        if col not in df.columns:
            df[col] = val
    df["quantity"]     = pd.to_numeric(df["quantity"],     errors="coerce").fillna(1).astype(int)
    df["is_automated"] = pd.to_numeric(df["is_automated"], errors="coerce").fillna(0).astype(int)
    return df

# ---------------------------------------------------------------------------
# 衍生報表函式（全部基於 _load_raw_txns()，與資料源無關）
# ---------------------------------------------------------------------------

def load_summary(df: pd.DataFrame) -> dict:
    if df.empty:
        return {"total_orders": 0, "total_qty": 0, "updated_at": None}
    return {
        "total_orders": len(df),
        "total_qty":    int(df["quantity"].sum()),
        "updated_at":   df["timestamp"].dropna().max() if "timestamp" in df.columns else None,
    }


def load_top10(df: pd.DataFrame) -> pd.DataFrame:
    """
    SQL 等效：
    SELECT user_id, country, SUM(quantity) as total_qty
    FROM transactions
    GROUP BY user_id
    ORDER BY total_qty DESC LIMIT 10
    """
    if df.empty or "user_id" not in df.columns:
        return pd.DataFrame()
    valid = df[df["user_id"].notna() & df["user_id"].astype(str).str.strip().ne("")]
    if valid.empty:
        return pd.DataFrame()
    agg = (
        valid.groupby("user_id", as_index=False)
        .agg(
            country     = ("country",      lambda x: x.dropna().iloc[0] if x.dropna().any() else "—"),
            total_qty   = ("quantity",     "sum"),
            order_count = ("order_id",     "count"),
            first_seen  = ("timestamp",    "min"),
            last_seen   = ("timestamp",    "max"),
            is_bot      = ("is_automated", "max"),
        )
        .sort_values(["total_qty", "first_seen"], ascending=[False, True])
        .head(10)
        .reset_index(drop=True)
    )
    agg["country"] = agg["country"].fillna("—")
    return agg


def load_my_total(df: pd.DataFrame) -> int:
    if not MY_USER_ID.strip() or df.empty:
        return 0
    mask = df["user_id"].astype(str) == MY_USER_ID.strip()
    return int(df.loc[mask, "quantity"].sum())


def load_top3_growth(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty or "user_id" not in df.columns:
        return pd.DataFrame()

    top10 = load_top10(df)
    if top10.empty:
        return pd.DataFrame()

    top3_ids = top10["user_id"].head(3).tolist()
    sub = df[df["user_id"].isin(top3_ids)].copy()
    sub["timestamp"] = pd.to_datetime(sub["timestamp"], errors="coerce")
    sub = sub.dropna(subset=["timestamp"])
    if sub.empty:
        return pd.DataFrame()

    series = {}
    for uid in top3_ids:
        udf = sub[sub["user_id"] == uid].sort_values("timestamp")
        cum = udf.set_index("timestamp")["quantity"].cumsum()
        label = (uid[:12] + "…") if len(uid) > 14 else uid
        series[label] = cum

    combined = pd.DataFrame(series).sort_index().ffill()
    return combined


def load_country_stats(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return pd.DataFrame()
    tmp = df.copy()
    tmp["country"] = tmp["country"].fillna("未知地區").replace("", "未知地區")
    agg = (
        tmp.groupby("country", as_index=False)
        .agg(order_count=("order_id", "count"), total_qty=("quantity", "sum"))
        .sort_values("total_qty", ascending=False)
        .reset_index(drop=True)
    )
    return agg

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
        ivs = [(times[i+1]-times[i]).total_seconds() for i in range(len(times)-1)]
        win = BOT_CONSECUTIVE - 1
        for i in range(len(ivs) - win + 1):
            if all(iv < BOT_INTERVAL_SECS for iv in ivs[i:i+win]):
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

    # ── 資料源狀態提示 ────────────────────────────────────────────────────────
    if _use_local():
        data_src = "🗄️ 本地 SQLite"
    elif cloud_db.enabled():
        data_src = "☁️ Supabase"
    else:
        data_src = "❌ 無可用資料源（請設定 SUPABASE_URL / SUPABASE_KEY）"

    # ── 載入原始資料（後續所有報表共用）─────────────────────────────────────
    raw = _ensure_cols(_load_raw_txns())

    # ── Header ───────────────────────────────────────────────────────────────
    st.title("🏆 累計競爭格局分析儀表板")
    st.caption(
        f"商品 #{GOODS_ID} · {PRODUCT_NAME} · "
        f"截止 {DEADLINE.strftime('%Y-%m-%d %H:%M')} · 資料源：{data_src}"
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
    summary  = load_summary(raw)
    top10_df = load_top10(raw)
    my_total = load_my_total(raw)

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
            st.metric("我的累計", my_total,
                      delta=f"-{gap_to_1}" if gap_to_1 > 0 else "領先！")
        else:
            st.metric("我的累計", "— (未設定)",
                      help="在 app.py 頂部設定 MY_USER_ID 後顯示")

    if summary["updated_at"]:
        st.caption(f"最後更新：{str(summary['updated_at'])[:19]}")

    st.markdown("---")

    # ════════════════════════════════════════════════════════════════════════
    # 門檻差距分析
    # ════════════════════════════════════════════════════════════════════════
    if not top10_df.empty:
        first_uid = str(top10_df.iloc[0]["user_id"])

        if MY_USER_ID.strip():
            if MY_USER_ID.strip() == first_uid:
                second_qty = int(top10_df.iloc[1]["total_qty"]) if len(top10_df) > 1 else 0
                st.success(
                    f"🥇 **你就是第一名！** 累計購買 **{my_total}** 張，"
                    f"領先第二名 **{first_qty - second_qty}** 張。"
                )
            elif my_total > 0:
                st.warning(
                    f"**第一名**（`{first_uid[:20]}…`）累計：**{first_qty}** 張　｜　"
                    f"**你的累計**：**{my_total}** 張　｜　"
                    f"🎯 距離第一還差：**{gap_to_1}** 張"
                )
            else:
                st.info(
                    f"**第一名**（`{first_uid[:20]}…`）累計：**{first_qty}** 張　｜　"
                    f"你的 ID `{MY_USER_ID}` 尚未在資料庫中出現。"
                )
        else:
            st.info(
                f"🥇 **第一名**（`{first_uid[:20]}…`）目前累計：**{first_qty}** 張　"
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
        bot_ids = detect_bot_users(raw)
        rows = []
        for rank, (_, r) in enumerate(top10_df.iterrows(), start=1):
            uid    = str(r["user_id"])
            is_bot = uid in bot_ids
            is_me  = bool(MY_USER_ID.strip()) and uid == MY_USER_ID.strip()
            note   = " 👤 我" if is_me else ("⚠️ Bot" if is_bot else "")
            rows.append({
                "排名":       _badge(rank, is_bot),
                "用戶 ID":    uid,
                "地區":       str(r["country"]),
                "累計購買張數": int(r["total_qty"]),
                "訂單筆數":   int(r["order_count"]),
                "首次出現":   str(r["first_seen"])[:16],
                "最後出現":   str(r["last_seen"])[:16],
                "備注":       note,
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
    growth_df = load_top3_growth(raw)

    if growth_df.empty:
        st.info("累計增長圖需要至少 1 筆交易資料後才會顯示。")
    else:
        st.markdown(
            "折線代表 **Top 3 用戶各自的累計購買張數**，斜率越陡代表購買節奏越快。"
        )
        st.line_chart(growth_df, use_container_width=True)

    st.markdown("---")

    # ════════════════════════════════════════════════════════════════════════
    # 銷售地區分布
    # ════════════════════════════════════════════════════════════════════════
    st.subheader("🌏 銷售地區分布")
    country_df = load_country_stats(raw)

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
        recent = raw.sort_values("timestamp", ascending=False).head(100) if not raw.empty else raw
        if recent.empty:
            st.warning(
                "transactions 表為空。\n\n"
                "監控器正在監聽 `yetimall.store` 的 API 回應。\n\n"
                f"資料源：{data_src}"
            )
        else:
            dt = recent.copy()
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
**Streamlit Cloud 必要 Secrets（Settings → Secrets）：**
```
SUPABASE_URL = https://dldxbxkjctonqgkrdpes.supabase.co
SUPABASE_KEY = <your anon key>
```

| 參數 | 目前值 | 說明 | 位置 |
|------|--------|------|------|
| `GOODS_ID` | `{GOODS_ID}` | 目標商品 ID | `config.py` |
| `MY_USER_ID` | `"{MY_USER_ID or '未設定'}"` | 你的用戶 ID | `app.py` 頂部 |
| `BOT_INTERVAL_SECS` | `{BOT_INTERVAL_SECS}` | Bot 偵測間隔閾值（秒）| `app.py` 頂部 |
| `AUTO_REFRESH_SECS` | `{AUTO_REFRESH_SECS}` | 頁面自動刷新（秒）| `app.py` 頂部 |
| 資料源 | `{data_src}` | 自動偵測 | — |

**模組職責：**
| 檔案 | 職責 |
|------|------|
| `config.py` | 平台切換（URL / 關鍵字 / 時間）|
| `processor.py` | JSON 解析 + DB 寫入 |
| `monitor.py` | Playwright 監聽器 |
| `cloud_db.py` | Supabase 讀寫（SQLite 不存在時自動啟用）|
| `app.py` | 競爭格局儀表板（只讀）|
        """)

    # ── Auto-refresh ─────────────────────────────────────────────────────────
    st.markdown(
        f"<meta http-equiv='refresh' content='{AUTO_REFRESH_SECS}'>",
        unsafe_allow_html=True,
    )


if __name__ == "__main__":
    main()
