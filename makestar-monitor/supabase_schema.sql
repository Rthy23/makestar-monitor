-- ============================================================
-- Makestar Monitor — Supabase schema
-- Run this once in your Supabase project's SQL Editor
-- (Dashboard → SQL Editor → New query → paste → Run)
-- ============================================================

CREATE TABLE IF NOT EXISTS stock_state (
    id                  INTEGER PRIMARY KEY,
    last_stock          INTEGER,
    last_is_purchasable INTEGER,
    last_sale_status    TEXT,
    updated_at          TEXT NOT NULL,
    source_url          TEXT
);

CREATE TABLE IF NOT EXISTS status_log (
    id                  BIGSERIAL PRIMARY KEY,
    timestamp           TEXT NOT NULL,
    is_purchasable      INTEGER,
    sale_status         TEXT,
    stock               INTEGER,
    participation_count INTEGER,
    poll_interval       REAL,
    is_silent_mode      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS transactions (
    id        BIGSERIAL PRIMARY KEY,
    timestamp TEXT    NOT NULL,
    quantity  INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS participants (
    participant_id    BIGSERIAL PRIMARY KEY,
    first_purchase_at TEXT    NOT NULL,
    total_quantity    INTEGER NOT NULL DEFAULT 0
);
