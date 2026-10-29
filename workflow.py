import json
from pathlib import Path
import sqlite3
from engine import number
ROOT=Path(__file__).resolve().parent

def connect(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    db = sqlite3.connect(path)
    path.chmod(0o600)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
      CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, ts INTEGER, version TEXT, config TEXT);
      CREATE TABLE IF NOT EXISTS scans(run_id TEXT, inst_id TEXT, result TEXT, PRIMARY KEY(run_id, inst_id));
      CREATE TABLE IF NOT EXISTS oi(inst_id TEXT, ts INTEGER, value REAL, PRIMARY KEY(inst_id, ts));
      CREATE TABLE IF NOT EXISTS plans(id TEXT PRIMARY KEY, created INTEGER, expires INTEGER, plan TEXT, outcome TEXT DEFAULT 'PENDING');
      CREATE TABLE IF NOT EXISTS email_outbox(id TEXT PRIMARY KEY, created INTEGER, expires INTEGER, payload TEXT, state TEXT DEFAULT 'PENDING');
    """)
    return db

def oi_evidence(db, item, now_ms, bar_ms):
    if not item:
        return {"status": "UNAVAILABLE"}
    value, ts = number(item["oi"]), int(item["ts"])
    if value < 0 or not 0 <= now_ms - ts <= 120_000:
        return {"status": "STALE_OR_INVALID"}
    previous = db.execute("SELECT ts,value FROM oi WHERE inst_id=? AND ts<=? ORDER BY ts DESC LIMIT 1",
                          (item["instId"], ts - bar_ms)).fetchone()
    db.execute("INSERT OR IGNORE INTO oi VALUES(?,?,?)", (item["instId"], ts, value))
    if not previous or previous[1] <= 0 or ts - previous[0] > bar_ms * 2:
        return {"status": "WARMUP", "oi": value, "ts": ts}
    return {"status": "AVAILABLE", "oi": value, "ts": ts, "baseline_ts": previous[0],
            "elapsed_ms": ts - previous[0], "delta_pct": (value / previous[1] - 1) * 100,
            "meaning": "contract_open_interest_change_not_capital_net_flow"}

def outcome(plan, candles, now_ms):
    """Price-touch observation only, not a fill model or realized win/loss."""
    start, end = plan["created_at"], min(plan["expires_at"], now_ms)
    observations = sorted([r for r in candles if len(r) >= 9 and str(r[8]) == "1"
                           and start <= int(r[0]) and int(r[0]) + 60_000 <= end], key=lambda r: int(r[0]))
    expected_start = ((start + 59_999) // 60_000) * 60_000
    if expected_start + 60_000 > end:
        return "PENDING" if now_ms < plan["expires_at"] else "INSUFFICIENT_HISTORY"
    if not observations or int(observations[0][0]) != expected_start:
        return "INSUFFICIENT_HISTORY"
    for i, r in enumerate(observations):
        if i and int(r[0]) - int(observations[i - 1][0]) != 60_000:
            return "INSUFFICIENT_HISTORY"
        high, low = number(r[2]), number(r[3])
        stop = low <= plan["stop"] if plan["side"] == "long" else high >= plan["stop"]
        target = high >= plan["target"] if plan["side"] == "long" else low <= plan["target"]
        if stop and target:
            return "AMBIGUOUS_BOTH_TOUCHED"
        if stop:
            return "STOP_TOUCHED"
        if target:
            return "TARGET_TOUCHED"
    if int(observations[-1][0]) + 60_000 < (end // 60_000) * 60_000:
        return "INSUFFICIENT_HISTORY"
    return "EXPIRED_NO_TOUCH" if now_ms >= plan["expires_at"] else "PENDING"
