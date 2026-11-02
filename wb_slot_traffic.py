"""Proxy slot traffic and token consumption tracker with SQLite persistence.

Tracks requests, prompt/completion/total tokens, and in/out/total bytes
aggregated per proxy slot per hour. Supports querying by hour, day,
month, or all-time totals, plus idempotent historical backfill from usage.jsonl.
"""

import glob
import json
import os
import sqlite3
import threading
import time
import atexit

_lock = threading.Lock()
_conns = {}


def _get_conn(usage_dir):
    """Return a thread-safe SQLite connection for the given usage_dir."""
    key = os.path.abspath(usage_dir)
    with _lock:
        if key in _conns:
            return _conns[key]
        os.makedirs(key, exist_ok=True)
        db_path = os.path.join(key, "slot_traffic.sqlite3")
        conn = sqlite3.connect(db_path, check_same_thread=False, timeout=10.0)
        conn.row_factory = sqlite3.Row
        with conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS slot_traffic (
                    slot_id TEXT NOT NULL,
                    hour TEXT NOT NULL,
                    requests INTEGER NOT NULL DEFAULT 0,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    bytes_in INTEGER NOT NULL DEFAULT 0,
                    bytes_out INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (slot_id, hour)
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_slot_traffic_hour ON slot_traffic(hour)")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS slot_traffic_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT
                )
            """)
        _conns[key] = conn
        return conn


def init(usage_dir):
    """Initialize the SQLite database and return its file path."""
    if not usage_dir:
        return None
    conn = _get_conn(usage_dir)
    return os.path.join(os.path.abspath(usage_dir), "slot_traffic.sqlite3")


def record(usage_dir, slot_id, prompt_tokens=0, completion_tokens=0, total_tokens=0,
           bytes_in=0, bytes_out=0, ts=None):
    """Record traffic and tokens for a given proxy slot.

    If bytes are zero but tokens exist, estimates bytes based on token counts
    (3.5 bytes/prompt token + 400 headers, 4.0 bytes/completion token + 300 sse).
    """
    if not usage_dir or not slot_id:
        return
    slot_id = str(slot_id).strip()
    if not slot_id:
        return

    pt = int(prompt_tokens or 0)
    ct = int(completion_tokens or 0)
    tt = int(total_tokens or (pt + ct))

    bi = int(bytes_in or 0)
    bo = int(bytes_out or 0)
    if bi <= 0 and pt > 0:
        bi = int(pt * 3.5 + 400)
    if bo <= 0 and ct > 0:
        bo = int(ct * 4.0 + 300)

    if ts is None:
        ts = time.time()
    hour = time.strftime("%Y-%m-%d-%H", time.localtime(ts))

    conn = _get_conn(usage_dir)
    with _lock:
        with conn:
            conn.execute("""
                INSERT INTO slot_traffic (slot_id, hour, requests, prompt_tokens, completion_tokens, total_tokens, bytes_in, bytes_out)
                VALUES (?, ?, 1, ?, ?, ?, ?, ?)
                ON CONFLICT(slot_id, hour) DO UPDATE SET
                    requests = requests + 1,
                    prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                    completion_tokens = completion_tokens + excluded.completion_tokens,
                    total_tokens = total_tokens + excluded.total_tokens,
                    bytes_in = bytes_in + excluded.bytes_in,
                    bytes_out = bytes_out + excluded.bytes_out
            """, (slot_id, hour, pt, ct, tt, bi, bo))


def query(usage_dir, interval="all", slot_id=None, limit=None, period=None):
    """Query aggregated traffic and token statistics.

    interval: 'all' | 'month' | 'day' | 'hour'
    slot_id: optional filter for a single slot (e.g. 'slot-1')
    limit: optional max count of series periods returned
    period: optional target period string (e.g. '2026-11'); defaults to current period
    """
    if not usage_dir:
        return {"interval": interval, "period": "all", "totals": {}, "period_totals": {}, "series": []}

    conn = _get_conn(usage_dir)
    params = []
    where_slot = ""
    if slot_id:
        where_slot = " WHERE slot_id = ?"
        params.append(str(slot_id).strip())

    with _lock:
        # 1. Compute grand totals per slot
        totals_sql = f"""
            SELECT slot_id,
                   SUM(requests) AS requests,
                   SUM(prompt_tokens) AS prompt_tokens,
                   SUM(completion_tokens) AS completion_tokens,
                   SUM(total_tokens) AS total_tokens,
                   SUM(bytes_in) AS bytes_in,
                   SUM(bytes_out) AS bytes_out
            FROM slot_traffic
            {where_slot}
            GROUP BY slot_id
            ORDER BY slot_id
        """
        totals = {}
        for row in conn.execute(totals_sql, params).fetchall():
            sid = row["slot_id"]
            bi = int(row["bytes_in"] or 0)
            bo = int(row["bytes_out"] or 0)
            totals[sid] = {
                "requests": int(row["requests"] or 0),
                "prompt_tokens": int(row["prompt_tokens"] or 0),
                "completion_tokens": int(row["completion_tokens"] or 0),
                "total_tokens": int(row["total_tokens"] or 0),
                "bytes_in": bi,
                "bytes_out": bo,
                "bytes_total": bi + bo,
            }

        # 2. Compute series if interval != 'all'
        series = []
        target_period = "all"
        period_totals = totals
        if interval in ("month", "day", "hour"):
            if interval == "month":
                sub_len = 7   # YYYY-MM
                cur_period = time.strftime("%Y-%m")
            elif interval == "day":
                sub_len = 10  # YYYY-MM-DD
                cur_period = time.strftime("%Y-%m-%d")
            else:
                sub_len = 13  # YYYY-MM-DD-HH
                cur_period = time.strftime("%Y-%m-%d-%H")

            target_period = str(period).strip() if period else cur_period

            series_sql = f"""
                SELECT substr(hour, 1, {sub_len}) AS period,
                       slot_id,
                       SUM(requests) AS requests,
                       SUM(prompt_tokens) AS prompt_tokens,
                       SUM(completion_tokens) AS completion_tokens,
                       SUM(total_tokens) AS total_tokens,
                       SUM(bytes_in) AS bytes_in,
                       SUM(bytes_out) AS bytes_out
                FROM slot_traffic
                {where_slot}
                GROUP BY period, slot_id
                ORDER BY period ASC, slot_id ASC
            """
            rows = conn.execute(series_sql, params).fetchall()
            by_period = {}
            period_order = []
            for r in rows:
                p = r["period"]
                if p not in by_period:
                    by_period[p] = {}
                    period_order.append(p)
                sid = r["slot_id"]
                bi = int(r["bytes_in"] or 0)
                bo = int(r["bytes_out"] or 0)
                by_period[p][sid] = {
                    "requests": int(r["requests"] or 0),
                    "prompt_tokens": int(r["prompt_tokens"] or 0),
                    "completion_tokens": int(r["completion_tokens"] or 0),
                    "total_tokens": int(r["total_tokens"] or 0),
                    "bytes_in": bi,
                    "bytes_out": bo,
                    "bytes_total": bi + bo,
                }
            if limit and isinstance(limit, int) and limit > 0:
                period_order = period_order[-limit:]
            for p in period_order:
                series.append({"period": p, "slots": by_period[p]})

            period_totals = by_period.get(target_period, {})

        return {
            "interval": interval,
            "period": target_period,
            "totals": totals,
            "period_totals": period_totals,
            "series": series
        }


def backfill_from_usage(usage_dir, accounts_dir, force=False):
    """Scan usage.jsonl and backfill slot traffic statistics idempotently.

    Estimates bytes_in / bytes_out based on prompt and completion tokens
    when physical bytes were not recorded in historical logs.
    Returns the count of backfilled request records.
    """
    if not usage_dir or not accounts_dir:
        return 0

    conn = _get_conn(usage_dir)
    with _lock:
        if not force:
            cur = conn.execute("SELECT value FROM slot_traffic_meta WHERE key = 'backfill_done'")
            row = cur.fetchone()
            if row and row["value"] == "1":
                return 0

    # 1. Build account UID -> proxy_slot map
    acct_to_slot = {}
    if os.path.exists(accounts_dir):
        for path in glob.glob(os.path.join(accounts_dir, "*.json")):
            base = os.path.basename(path)
            if base in ("settings.json", "model.json", "model_windows.json",
                        "active_realm.json", "quota_scan.json"):
                continue
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    if not isinstance(data, dict):
                        continue
                    uid = data.get("uid") or os.path.splitext(base)[0]
                    slot = data.get("proxySlot") or data.get("proxy_slot")
                    if slot:
                        acct_to_slot[uid] = str(slot).strip()
            except Exception:
                pass

        # Also inspect CodeArts accounts if stored
        ca_path = os.path.join(accounts_dir, "codearts.json")
        if os.path.exists(ca_path):
            try:
                with open(ca_path, "r", encoding="utf-8") as f:
                    ca_data = json.load(f)
                    for acct in ca_data.get("accounts") or []:
                        uid = acct.get("uid")
                        slot = acct.get("proxySlot") or acct.get("proxy_slot")
                        if uid and slot:
                            acct_to_slot[uid] = str(slot).strip()
            except Exception:
                pass

    if not acct_to_slot:
        return 0

    usage_log = os.path.join(usage_dir, "usage.jsonl")
    if not os.path.exists(usage_log):
        return 0

    # 2. Pre-aggregate in memory: (slot_id, hour) -> [requests, pt, ct, tt, bi, bo]
    accum = {}
    matched_count = 0
    try:
        with open(usage_log, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                acct = row.get("account")
                slot = acct_to_slot.get(acct)
                if not slot:
                    continue

                ts = row.get("at")
                if not ts:
                    continue
                hour = time.strftime("%Y-%m-%d-%H", time.localtime(ts))

                pt = int(row.get("prompt_tokens") or 0)
                ct = int(row.get("completion_tokens") or 0)
                tt = int(row.get("total_tokens") or (pt + ct))

                bi = int(row.get("bytes_in") or int(pt * 3.5 + 400))
                bo = int(row.get("bytes_out") or int(ct * 4.0 + 300))

                key = (slot, hour)
                if key not in accum:
                    accum[key] = [0, 0, 0, 0, 0, 0]
                rec = accum[key]
                rec[0] += 1
                rec[1] += pt
                rec[2] += ct
                rec[3] += tt
                rec[4] += bi
                rec[5] += bo
                matched_count += 1
    except Exception:
        pass

    # 3. Batch commit to SQLite
    with _lock:
        with conn:
            if force:
                conn.execute("DELETE FROM slot_traffic")
            for (slot, hour), (req, pt, ct, tt, bi, bo) in accum.items():
                conn.execute("""
                    INSERT INTO slot_traffic (slot_id, hour, requests, prompt_tokens, completion_tokens, total_tokens, bytes_in, bytes_out)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(slot_id, hour) DO UPDATE SET
                        requests = requests + excluded.requests,
                        prompt_tokens = prompt_tokens + excluded.prompt_tokens,
                        completion_tokens = completion_tokens + excluded.completion_tokens,
                        total_tokens = total_tokens + excluded.total_tokens,
                        bytes_in = bytes_in + excluded.bytes_in,
                        bytes_out = bytes_out + excluded.bytes_out
                """, (slot, hour, req, pt, ct, tt, bi, bo))
            conn.execute("""
                INSERT INTO slot_traffic_meta (key, value) VALUES ('backfill_done', '1')
                ON CONFLICT(key) DO UPDATE SET value = '1'
            """)

    return matched_count


def close(usage_dir=None):
    """Close open database connections (useful during tests or shutdown)."""
    global _conns
    with _lock:
        if usage_dir:
            key = os.path.abspath(usage_dir)
            conn = _conns.pop(key, None)
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass
        else:
            for conn in _conns.values():
                try:
                    conn.close()
                except Exception:
                    pass
            _conns.clear()


atexit.register(close)


