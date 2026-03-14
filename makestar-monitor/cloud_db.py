"""
cloud_db.py — Supabase REST API adapter for Makestar monitor

Set these environment variables to enable cloud sync:
  SUPABASE_URL = https://xxxx.supabase.co
  SUPABASE_KEY = <anon public key>

When not set, all write functions are silent no-ops and all read
functions return None (callers fall back to local SQLite).
"""

import os
import logging

import requests

logger = logging.getLogger(__name__)

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_KEY = os.environ.get("SUPABASE_KEY", "")


def enabled() -> bool:
    return bool(SUPABASE_URL and SUPABASE_KEY)


def _h(prefer: str = None) -> dict:
    h = {
        "apikey": SUPABASE_KEY,
        "Authorization": f"Bearer {SUPABASE_KEY}",
        "Content-Type": "application/json",
    }
    if prefer:
        h["Prefer"] = prefer
    return h


# ---------------------------------------------------------------------------
# Write helpers (called by monitor.py)
# ---------------------------------------------------------------------------

def write_status_log(timestamp, is_purchasable, sale_status, stock,
                     participation_count, poll_interval, is_silent_mode):
    if not enabled():
        return
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/status_log",
            headers=_h("return=minimal"),
            json={
                "timestamp":           timestamp,
                "is_purchasable":      is_purchasable,
                "sale_status":         sale_status,
                "stock":               stock,
                "participation_count": participation_count,
                "poll_interval":       poll_interval,
                "is_silent_mode":      is_silent_mode,
            },
            timeout=5,
        )
    except Exception as exc:
        logger.debug("Supabase write_status_log: %s", exc)


def write_stock_state(last_stock, last_is_purchasable, last_sale_status,
                      updated_at, source_url):
    if not enabled():
        return
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/stock_state",
            headers=_h("resolution=merge-duplicates,return=minimal"),
            json={
                "id":                   1,
                "last_stock":           last_stock,
                "last_is_purchasable":  last_is_purchasable,
                "last_sale_status":     last_sale_status,
                "updated_at":           updated_at,
                "source_url":           source_url,
            },
            timeout=5,
        )
    except Exception as exc:
        logger.debug("Supabase write_stock_state: %s", exc)


def write_transaction(timestamp: str, quantity: int):
    if not enabled():
        return
    try:
        requests.post(
            f"{SUPABASE_URL}/rest/v1/transactions",
            headers=_h("return=minimal"),
            json={"timestamp": timestamp, "quantity": quantity},
            timeout=5,
        )
        requests.post(
            f"{SUPABASE_URL}/rest/v1/participants",
            headers=_h("return=minimal"),
            json={"first_purchase_at": timestamp, "total_quantity": quantity},
            timeout=5,
        )
    except Exception as exc:
        logger.debug("Supabase write_transaction: %s", exc)


# ---------------------------------------------------------------------------
# Read helpers (called by app.py on Streamlit Cloud)
# ---------------------------------------------------------------------------

def _get_list(path: str, params: dict) -> list | None:
    """GET a PostgREST endpoint and return a list, or None on any error."""
    if not enabled():
        return None
    try:
        r = requests.get(
            f"{SUPABASE_URL}/rest/v1/{path}",
            headers=_h(),
            params=params,
            timeout=10,
        )
        data = r.json()
        if isinstance(data, list):
            return data
        logger.debug("Supabase %s returned non-list: %s", path, data)
        return None
    except Exception as exc:
        logger.debug("Supabase read %s: %s", path, exc)
        return None


def read_status_log(limit: int = 200):
    """Return list-of-dicts or None on error / not enabled."""
    return _get_list("status_log", {"order": "timestamp.desc", "limit": limit})


def read_stock_state():
    """Return single-row dict or None."""
    rows = _get_list("stock_state", {"id": "eq.1", "limit": 1})
    return rows[0] if rows else None


def read_transactions():
    """Return list-of-dicts with 'quantity' key, or None."""
    return _get_list("transactions", {"select": "quantity"})


def read_participants():
    """Return list-of-dicts ordered by rank, or None."""
    return _get_list("participants", {"order": "total_quantity.desc,first_purchase_at.asc"})
