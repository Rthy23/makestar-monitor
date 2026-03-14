# Makestar 簽售活動銷售監控系統

## 概述
即時監控 Makestar 簽售活動（Campaign ID: 16183 — aespa《ALPHA DRIVE ONE》1st mini album 簽售）的銷售狀態。每次請求都使用隨機 User-Agent，並根據時間自動調整採樣頻率，直到 2026-03-16 00:00 截止。

## 工作流程

| 工作流程 | 指令 | 端口 |
|---|---|---|
| Makestar Monitor (Streamlit Dashboard) | `cd makestar-monitor && python3 -m streamlit run app.py ...` | 5000 |
| Makestar Stock Monitor | `cd makestar-monitor && python3 monitor.py` | — |

## 目錄結構

```
makestar-monitor/
├── monitor.py              # 主監控迴圈（自適應採樣）
├── app.py                  # Streamlit 儀表板（含趨勢預測）
├── makestar_monitor.db     # SQLite 資料庫
└── scrapers/
    └── browser_fetcher.py  # 三段式抓取策略 + UA 輪替
```

## 智慧型自適應採樣策略

| 時段 | 採樣間隔 | 觸發條件 |
|---|---|---|
| 常規期 | 30 秒 + jitter | 活動期間默認 |
| 高頻期 | 5 秒 + jitter | 2026-03-15 21:00 起（截止前 3h） |
| 靜默期 | 60 秒 + jitter | API 返回 429，自動恢復 |
| jitter | +1.0~3.0 秒隨機 | 每次請求均加入 |

- 每次請求都重新生成 User-Agent（10 種 UA 池輪替）
- 連續 3 次失敗後切換為 Playwright 備用策略

## 資料抓取策略（按優先順序）

1. **直接 JSON API**（~0.2秒）
   `https://new-commerce-api.makestar.com/v2/commerce/product_event/{id}/dynamic/`
   → 返回 `saleStatus`, `isPurchasable`, `isDisplayStock`

2. **SSR HTML 解析**（~1-2秒）
   `https://www.makestar.com/product/{id}` → Nuxt 3 revive payload 解碼

3. **Playwright 無頭瀏覽器**（~10秒，備用）
   系統 Chromium (`/nix/store/…/bin/chromium`) + XHR 攔截

## 資料庫結構（SQLite）

- `stock_state` — 最新狀態快照（id=1 單列）
- `transactions` — 偵測到的購買事件
- `participants` — 累計參與者記錄
- `status_log` — 每次輪詢的完整歷史，包含：
  - `participation_count` — 從 participants 表累計的追蹤人數
  - `poll_interval` — 當次使用的採樣間隔
  - `is_silent_mode` — 是否處於 429 靜默模式

## 儀表板功能（app.py）

- 採樣模式badge（常規 / 高頻 / 靜默警告）
- 即時狀態指標：`isPurchasable`, `saleStatus`, 庫存
- **保位門檻預測**：
  - 根據過去 1 小時增長率線性外插至截止時間
  - 顯示前 15、30、50、100、150、200 名門檻估算
  - 追蹤數不足時顯示說明訊息
- 參與數趨勢圖（當有數據時）
- 參與者排名表：藍色(1-50)、綠色(51-150)、白色(151+)
- 狀態變化紀錄（含採樣間隔、模式欄位）
- 每 10 秒自動刷新

## 已知限制

- `isDisplayStock = false` — Makestar 不顯示庫存數值，系統追蹤 `isPurchasable` 狀態
- `participationCount` API 未公開，系統以自建 `participants` 表的累計數作為替代
- 當庫存可見時，系統會自動偵測差值並記錄購買事件

## 依賴套件

- Python 3.11
- streamlit, requests, pandas, playwright
- 系統套件：chromium, nspr, nss（透過 Nix 安裝）
