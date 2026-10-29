"""Deterministic, network-free spot/swap signal and risk calculations."""
from __future__ import annotations

from decimal import Decimal, ROUND_DOWN, ROUND_CEILING
import hashlib
import math
from position_adapter import fixed_fractional

VERSION = "0.2.0"
BARS = {"5m": 300_000, "15m": 900_000, "1H": 3_600_000,
        "4H": 14_400_000, "6H": 21_600_000, "1D": 86_400_000}


def number(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Non-finite number")
    return result


def ema_series(values, period):
    if len(values) < period:
        return [None] * len(values)
    result = [None] * (period - 1) + [sum(values[:period]) / period]
    for value in values[period:]:
        result.append(value * 2 / (period + 1) + result[-1] * (1 - 2 / (period + 1)))
    return result


def indicators(rows):
    closes, volumes = [r[4] for r in rows], [r[5] for r in rows]
    fast, slow = ema_series(closes, 12), ema_series(closes, 26)
    macd = [a - b for a, b in zip(fast, slow) if a is not None and b is not None]
    histogram = macd[-1] - ema_series(macd, 9)[-1]
    changes = [b - a for a, b in zip(closes, closes[1:])]
    gain = sum(max(x, 0) for x in changes[:14]) / 14
    loss = sum(max(-x, 0) for x in changes[:14]) / 14
    for change in changes[14:]:
        gain = (gain * 13 + max(change, 0)) / 14
        loss = (loss * 13 + max(-change, 0)) / 14
    rsi = 100 if loss == 0 else 100 - 100 / (1 + gain / loss)
    prior_volume = sum(volumes[-21:-1]) / 20
    return {"ma20": sum(closes[-20:]) / 20, "macd_hist": histogram, "rsi": rsi,
            "vol_ratio": volumes[-1] / prior_volume if prior_volume else 0,
            "price_change_pct": (rows[-1][4] / rows[-1][1] - 1) * 100}


def analyze(inst_id, raw, cfg, now_ms, market="swap"):
    """Only confirmed, contiguous, recent bars; never manufacture missing data."""
    bar_ms = BARS[cfg["bar"]]
    base = {"inst_id": inst_id, "signal": False, "side": None, "score": 0,
            "version": VERSION, "market": market, "bar": cfg["bar"]}
    if len({int(r[0]) for r in raw}) != len(raw):
        return {**base, "reason": "DUPLICATE_CANDLES"}
    rows = []
    for r in raw:
        if len(r) < 9:
            return {**base, "reason": "CONFIRM_FIELD_MISSING"}
        if str(r[8]) != "1":
            continue
        ts = int(r[0])
        values = [number(v) for v in r[1:6]]
        o, h, low, c, vol = values
        if min(o, h, low, c) <= 0 or vol < 0 or low > min(o, c) or h < max(o, c):
            return {**base, "reason": "INVALID_OHLCV"}
        offset = 28_800_000 if cfg["bar"] in ("6H", "1D") else 0
        if (ts + offset) % bar_ms or ts + bar_ms > now_ms:
            return {**base, "reason": "INVALID_CANDLE_TIME"}
        rows.append([ts, *values])
    rows.sort(key=lambda r: r[0])
    if len(rows) < 35:
        return {**base, "reason": "INSUFFICIENT_CONFIRMED_CANDLES"}
    if any(b[0] - a[0] != bar_ms for a, b in zip(rows, rows[1:])):
        return {**base, "reason": "CANDLE_GAP"}
    closed_at = rows[-1][0] + bar_ms
    if now_ms - closed_at > bar_ms:
        return {**base, "reason": "STALE_CANDLES"}
    ind = indicators(rows)
    close = rows[-1][4]
    long = {"ma": close > ind["ma20"], "macd": ind["macd_hist"] > 0,
            "change": ind["price_change_pct"] >= cfg["price_change_min"],
            "volume": ind["vol_ratio"] >= cfg["vol_ratio_min"],
            "rsi": cfg["rsi_long"][0] <= ind["rsi"] <= cfg["rsi_long"][1]}
    short = {"ma": close < ind["ma20"], "macd": ind["macd_hist"] < 0,
             "change": ind["price_change_pct"] <= -cfg["price_change_min"],
             "volume": long["volume"],
             "rsi": cfg["rsi_short"][0] <= ind["rsi"] <= cfg["rsi_short"][1]}
    side = "long" if all(long.values()) else "short" if market == "swap" and all(short.values()) else None
    direction = 1 if side == "long" else -1 if side == "short" else 0
    score = (min(ind["vol_ratio"] / cfg["vol_ratio_min"] * 3, 5) +
             min(direction * ind["price_change_pct"] / cfg["price_change_min"] * 3, 5)) if side else 0
    signal_id = hashlib.sha256(f"{VERSION}:{market}:{inst_id}:{cfg['bar']}:{rows[-1][0]}:{side}".encode()).hexdigest()[:24]
    return {**base, **ind, "signal": side is not None, "side": side, "score": score,
            "close": close, "candle_ts": rows[-1][0], "closed_at": closed_at,
            "signal_id": signal_id, "long_checks": long, "short_checks": short,
            "reason": "MATCH" if side else "FILTERED"}


def floor_step(value, step):
    return float((Decimal(str(value)) / Decimal(str(step))).to_integral_value(rounding=ROUND_DOWN) * Decimal(str(step)))


def ceil_step(value, step):
    return float((Decimal(str(value)) / Decimal(str(step))).to_integral_value(rounding=ROUND_CEILING) * Decimal(str(step)))


def make_plan(signal, instrument, ticker, cfg, risk):
    """Creates research plans only. Missing user budgets prevent sizing."""
    side = signal["side"]
    entry = number(ticker["askPx"] if side == "long" else ticker["bidPx"])
    stop_distance = entry * cfg["stop_loss_pct"] / 100
    tick = number(instrument["tickSz"])
    if tick <= 0:
        raise ValueError("Invalid tick size")
    # Long SL rounds toward entry (up); short SL toward entry (down).
    stop_raw = entry - stop_distance if side == "long" else entry + stop_distance
    stop = ceil_step(stop_raw, tick) if side == "long" else floor_step(stop_raw, tick)
    distance = abs(entry - stop)
    if distance <= 0:
        raise ValueError("Stop rounds to entry")
    target = floor_step(entry + distance * cfg["reward_risk"], tick) if side == "long" else ceil_step(entry - distance * cfg["reward_risk"], tick)
    if target <= 0:
        raise ValueError("Invalid target")
    plan = {"signal_id": signal["signal_id"], "inst_id": signal["inst_id"], "side": side,
            "entry_reference": entry, "stop": stop, "target": target,
            "expires_at": signal["closed_at"] + BARS[cfg["bar"]] * cfg["plan_expiry_bars"],
            "stop_method": "fixed_percentage_from_original_spot_skill",
            "status": "RESEARCH_ONLY", "quantity": None, "blockers": [],
            "executable": False}
    required = ["equity_usdt", "risk_pct", "leverage", "max_total_risk_pct", "max_gross_pct",
                "existing_risk_usdt", "existing_gross_usdt", "available_margin_usdt",
                "taker_fee_rate", "slippage_bps", "funding_cost_pct"]
    missing = [k for k in required if risk.get(k) is None]
    if missing:
        plan["blockers"] = ["MISSING_RISK_INPUT:" + k for k in missing]
        return plan
    vals = {k: number(risk[k]) for k in required}
    if any(v < 0 for v in vals.values()) or vals["equity_usdt"] <= 0 or vals["leverage"] < 1:
        raise ValueError("Invalid risk input")
    if not 0 < vals["risk_pct"] <= 100 or not 0 < vals["max_total_risk_pct"] <= 100:
        raise ValueError("Invalid risk percentage")
    if vals["taker_fee_rate"] >= 1:
        raise ValueError("Invalid fee rate")
    if signal["market"] == "swap":
        if instrument.get("ctType") != "linear" or instrument.get("settleCcy") != "USDT":
            raise ValueError("Only linear USDT contracts supported")
        coin = instrument.get("ctValCcy")
        if not coin or not signal["inst_id"].startswith(coin + "-"):
            raise ValueError("Contract value currency is not base currency")
        unit = number(instrument["ctVal"]) * number(instrument.get("ctMult") or 1)
    else:
        if vals["leverage"] != 1:
            raise ValueError("Spot research requires leverage=1")
        unit = 1
    lot, minimum = number(instrument["lotSz"]), number(instrument["minSz"])
    if min(unit, lot, minimum) <= 0:
        raise ValueError("Invalid instrument lot/contract value")
    slip = vals["slippage_bps"] / 10_000
    fee = vals["taker_fee_rate"]
    cost_per_unit = unit * (entry + stop) * (fee + slip) + entry * unit * vals["funding_cost_pct"] / 100
    loss_per_unit = distance * unit + cost_per_unit
    eq = vals["equity_usdt"]
    baseline = fixed_fractional(eq, vals["risk_pct"], entry, stop, side, unit)
    budget = min(eq * vals["risk_pct"] / 100, baseline["dollar_risk"], number(risk.get("max_loss_usdt", 20)),
                 max(0, eq * vals["max_total_risk_pct"] / 100 - vals["existing_risk_usdt"]))
    if budget < 0:
        raise ValueError("Invalid absolute loss cap")
    notional_remaining = max(0, eq * vals["max_gross_pct"] / 100 - vals["existing_gross_usdt"])
    # Reserve entry fee/slippage along with margin, without assuming netting.
    quantity = floor_step(min(budget / loss_per_unit, notional_remaining / (entry * unit),
                              vals["available_margin_usdt"] / (entry * unit * (1 / vals["leverage"] + fee + slip))), lot)
    if quantity < minimum:
        plan["blockers"].append("BELOW_MIN_SIZE_OR_NO_RISK_CAPACITY")
        return plan
    net_reward = abs(target - entry) * unit - cost_per_unit
    net_rr = net_reward / loss_per_unit
    if net_rr < 1:
        plan["blockers"].append("NET_REWARD_RISK_BELOW_ONE")
        return plan
    plan.update(quantity=quantity, quantity_unit="contracts" if signal["market"] == "swap" else "base_coin",
                notional_usdt=quantity * entry * unit, risk_usdt=quantity * loss_per_unit,
                margin_reserve_usdt=quantity * entry * unit * (1 / vals["leverage"] + fee + slip),
                net_reward_risk=net_rr, risk_inputs=vals,
                loss_budget_usdt=budget, environment="demo", sizing_method="position-sizer fixed-fractional + cost/lot/portfolio caps",
                position_sizer_version="2.0", baseline_risk_usdt=baseline["dollar_risk"],
                blockers=["DEMO_MANUAL_ORDER_AND_PROTECTION_RECHECK_REQUIRED"])
    return plan


def trend_direction(raw, bar, cfg, now_ms):
    """Use the same validated data and indicator formulas on the higher timeframe."""
    local = {**cfg, "bar": bar}
    result = analyze("TREND", raw, local, now_ms)
    if "close" not in result:
        return {"direction": "unknown", "reason": result["reason"], "bar": bar}
    close, ma, macd = result["close"], result["ma20"], result["macd_hist"]
    direction = "long" if close > ma and macd > 0 else "short" if close < ma and macd < 0 else "neutral"
    return {"direction": direction, "bar": bar, "close": close, "ma20": ma,
            "macd_hist": macd, "candle_ts": result["candle_ts"]}


def select_group(spec, ticker, cfg, now_ms):
    """Major symbols win; new listings or high-volume tagged meme/TradFi form fast group."""
    groups = cfg.get("groups")
    if not groups:
        return None
    symbol = spec["instId"].split("-")[0]
    if symbol in groups["major"]["symbols"]:
        return "major"
    fast = groups["fast"]
    listed = int(spec.get("listTime") or 0)
    new = listed > 0 and 0 <= now_ms - listed <= fast["new_listing_days"] * 86_400_000
    tagged = symbol in fast["meme_symbols"] or str(spec.get("instCategory")) in fast["tradfi_categories"]
    turnover = number(ticker.get("volCcy24h") or 0) * number(ticker.get("last") or 0)
    return "fast" if new or tagged and turnover >= fast["min_volume_usdt"] else None
