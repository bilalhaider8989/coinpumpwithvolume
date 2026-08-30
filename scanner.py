"""
Free Binance scanner - configurable DEMA200 + SuperTrend + MA9/20 cross,
with Discord notifications. Designed to run on a schedule via GitHub
Actions, so it works even when your own laptop is off.

Optional add-on: "scalp_mode" - a separate 5m early-scalp radar
(volume spike + green candle + DEMA200 trend + SuperTrend bullish),
toggled independently via CONFIG["scalp_mode"]["enabled"]. It does not
touch or depend on the original DEMA/MA-cross logic in any way.
"""

import json
import os
import time
from pathlib import Path

import pandas as pd
import requests

from indicators import dema, supertrend, moving_average

BINANCE_BASE = "https://data-api.binance.vision"
STATE_FILE = Path(__file__).parent / "scanner_state.json"

# ============================================================
# CONFIG - edit these to match your Pine script settings
# ============================================================

CONFIG = {
    # ---- 1. Timeframe (your main focus: 15m) ----
    "interval": "15m",          # 1m, 5m, 15m, 1h, 4h, 1d ...
    "candle_limit": 500,
    "quote_asset": "USDT",      # Binance-only, USDT pairs
    "request_sleep": 0.15,

    # ---- 3. DEMA200 filter (on/off + min % above) ----
    "dema": {
        "enabled": True,
        "length": 200,
        "min_pct_above": 0.3,   # set to 0 to allow ANY close above DEMA
    },

    # SuperTrend (used by the DEMA signal)
    "supertrend": {
        "atr_length": 12,
        "multiplier": 3.0,
    },

    # ---- 2. MA9/MA20 cross (configurable lengths) ----
    "ma_cross": {
        "enabled": True,
        "fast_length": 9,
        "slow_length": 20,
        "type": "EMA",           # SMA, EMA, or WMA
    },

    # ---- 4. Gap confirmation for the MA cross (on/off, % or StdDev) ----
    "ma_gap": {
        "mode": "Off",            # "Off", "% Gap", or "Std Dev"
        "min_pct": 0.3,
        "stdev_length": 20,
        "stdev_multiplier": 1.0,
    },

    # ---- Volume spike (MA9/20 cross only) ----
    # All three are REQUIRED when enabled: any one failing suppresses the
    # alert entirely. "Weight" is expressed as strictness, not a blended
    # score - the 15m multiplier is intentionally harder to clear than the
    # 1h one, so it dominates without needing an opaque formula.
    "volume_spike": {
        "enabled": True,

        # 15-minute spike: the fire candle vs its own recent baseline.
        # Primary signal - matches your scan cadence, hardest bar to clear.
        "lookback_15m": 10,       # candles used to build the baseline
        "multiplier_15m": 1.2,    # fire candle's volume must be >= this x baseline

        # 1-hour spike: sum of the last ~1h of candles vs the same-sized
        # window before it. Secondary confirmation - easier bar to clear,
        # so it has less influence than the 15m check.
        "lookback_1h": 10,        # prior 1h windows averaged for the baseline
        "multiplier_1h": 1.2,

        # 24h USDT liquidity floor - a hard minimum, not a spike check.
        # Fetched once per run for every symbol (1 API call, not per-symbol)
        # and applied BEFORE candles are even fetched, so illiquid pairs
        # never reach the signal logic at all.
        "min_24h_volume_usdt": 3_000_000,
    },

    # ---- Squeeze (MA9/20 cross only) ----
    # INFORMATIONAL when enabled: checks whether MA9/MA20 were tight together
    # for `lookback` candles right before the cross. Shown in the Discord
    # message ("squeeze=yes/no") but never blocks the alert.
    "squeeze": {
        "enabled": True,
        "lookback": 10,       # candles checked immediately before the cross
        "max_pct": 0.15,      # MA9/MA20 gap must stay <= this the whole window
    },

    # ---- SCALP MODE (new, independent layer) ----
    # Master switch: when False, this entire block is skipped and the
    # script behaves EXACTLY as before. When True, it runs as a separate
    # pass after the main scan and posts its own Discord message - it does
    # not read or write ma_armed/dema_trade_taken state at all.
    #
    # Goal: catch early scalps by ranking symbols on 5m volume spike, but
    # ONLY among candidates that also show a green candle / price-up move
    # AND are still trending (above DEMA200, SuperTrend bullish) - same
    # trend filter as your main DEMA signal, just applied on 5m candles.
    "scalp_mode": {
        "enabled": True,        # <-- flip this on/off

        "interval": "5m",
        "candle_limit": 500,     # needs >= dema_length*2 candles of history
        "top_n": 5,              # only the top N ranked candidates get posted

        # 5m volume spike: fire candle vs its own recent baseline
        "vol_lookback": 6,
        "vol_multiplier": 1.5,

        # Price confirmation: candle must close green, and move at least
        # this much (0.0 = any green candle qualifies)
        "min_price_change_pct": 0.0,

        # Trend filter, same idea as the main DEMA200+SuperTrend signal,
        # computed on 5m candles. Reuses CONFIG["supertrend"] params.
        "dema_length": 200,
        "dema_min_pct_above": 0.3,
    },

    # ---- 5. Discord notifications (free, no bot needed) ----
    "discord_webhook_url": os.environ.get("DISCORD_WEBHOOK_URL", ""),
}


# ============================================================
# Binance data fetching
# ============================================================

def _interval_minutes(interval: str) -> int:
    """'15m' -> 15, '1h' -> 60, '4h' -> 240, '1d' -> 1440."""
    unit = interval[-1]
    n = int(interval[:-1])
    return {"m": n, "h": n * 60, "d": n * 1440}[unit]


def get_usdt_symbols() -> list[str]:
    r = requests.get(f"{BINANCE_BASE}/api/v3/exchangeInfo", timeout=15)
    r.raise_for_status()
    data = r.json()
    return [
        s["symbol"]
        for s in data["symbols"]
        if s["quoteAsset"] == CONFIG["quote_asset"]
        and s["status"] == "TRADING"
        and s["isSpotTradingAllowed"]
    ]


def get_24h_volumes() -> dict[str, float]:
    """One call for EVERY symbol's rolling 24h USDT volume - used as a
    liquidity floor, not fetched per-symbol."""
    r = requests.get(f"{BINANCE_BASE}/api/v3/ticker/24hr", timeout=20)
    r.raise_for_status()
    return {d["symbol"]: float(d["quoteVolume"]) for d in r.json()}


def get_klines(symbol: str, interval: str | None = None, limit: int | None = None) -> pd.DataFrame:
    """interval/limit default to CONFIG["interval"]/CONFIG["candle_limit"]
    so all existing call sites behave exactly as before. Scalp mode passes
    its own interval/limit explicitly."""
    r = requests.get(
        f"{BINANCE_BASE}/api/v3/klines",
        params={
            "symbol": symbol,
            "interval": interval or CONFIG["interval"],
            "limit": limit or CONFIG["candle_limit"],
        },
        timeout=15,
    )
    r.raise_for_status()
    raw = r.json()
    df = pd.DataFrame(
        raw,
        columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore",
        ],
    )
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = df[col].astype(float)
    return df


# ============================================================
# State persistence (one-shot-per-episode memory across runs)
# ============================================================

def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ============================================================
# Discord notification
# ============================================================

def send_discord(message: str) -> None:
    url = CONFIG["discord_webhook_url"]
    if not url:
        print("  (Discord not configured - set DISCORD_WEBHOOK_URL env var / secret)")
        return
    try:
        requests.post(url, json={"content": message}, timeout=10)
    except requests.RequestException as e:
        print(f"  Discord send failed: {e}")


# ============================================================
# Per-symbol evaluation (ORIGINAL - unchanged)
# ============================================================

def check_symbol(symbol: str, state: dict, volume_24h: dict | None = None) -> list[dict]:
    """Returns a list of hit dicts (can contain 0, 1, or 2 signals per symbol)."""
    dema_cfg = CONFIG["dema"]
    ma_cfg = CONFIG["ma_cross"]
    gap_cfg = CONFIG["ma_gap"]
    vol_cfg = CONFIG["volume_spike"]
    squeeze_cfg = CONFIG["squeeze"]
    volume_24h = volume_24h or {}

    candles_per_hour = max(1, 60 // _interval_minutes(CONFIG["interval"]))

    min_history = max(
        dema_cfg["length"] * 2 if dema_cfg["enabled"] else 0,
        ma_cfg["slow_length"] + gap_cfg["stdev_length"] if ma_cfg["enabled"] else 0,
        ma_cfg["slow_length"] + squeeze_cfg["lookback"] if ma_cfg["enabled"] and squeeze_cfg["enabled"] else 0,
        vol_cfg["lookback_15m"] + 1 if vol_cfg["enabled"] else 0,
        candles_per_hour * (vol_cfg["lookback_1h"] + 1) if vol_cfg["enabled"] else 0,
        50,
    )

    df = get_klines(symbol)
    if len(df) < min_history:
        return []

    df = df.iloc[:-1]  # drop the still-forming candle
    close = df["close"]

    sym_state = state.get(symbol, {
        "dema_trade_taken": False,
        "ma_armed": False,
        "ma_gap_fired": False,
        "squeeze_ok": None,
    })

    hits = []

    # ---- DEMA200 + SuperTrend signal ----
    if dema_cfg["enabled"]:
        dema_val = dema(close, dema_cfg["length"])
        st = supertrend(df, CONFIG["supertrend"]["atr_length"], CONFIG["supertrend"]["multiplier"])

        last_close = close.iloc[-1]
        last_dema = dema_val.iloc[-1]
        distance_pct = (last_close - last_dema) / last_dema * 100
        armed = distance_pct >= dema_cfg["min_pct_above"]
        st_bullish = st["direction"].iloc[-1] < 0

        if not armed:
            sym_state["dema_trade_taken"] = False
        else:
            if not sym_state["dema_trade_taken"] and st_bullish:
                sym_state["dema_trade_taken"] = True
                hits.append({
                    "symbol": symbol,
                    "type": "DEMA200 + SuperTrend BUY",
                    "price": last_close,
                    "detail": f"DEMA200={last_dema:.8f}, {distance_pct:.2f}% above",
                })

    # ---- MA9/20 cross signal ----
    if ma_cfg["enabled"]:
        fast_ma = moving_average(close, ma_cfg["fast_length"], ma_cfg["type"])
        slow_ma = moving_average(close, ma_cfg["slow_length"], ma_cfg["type"])

        crossed_up = fast_ma.iloc[-2] <= slow_ma.iloc[-2] and fast_ma.iloc[-1] > slow_ma.iloc[-1]
        crossed_down = fast_ma.iloc[-2] >= slow_ma.iloc[-2] and fast_ma.iloc[-1] < slow_ma.iloc[-1]

        if crossed_up:
            sym_state["ma_armed"] = True
            sym_state["ma_gap_fired"] = False

            if squeeze_cfg["enabled"]:
                # Were MA9/MA20 tight together for `lookback` candles right
                # before this cross candle? (window excludes the cross candle
                # itself - squeeze describes what came before it)
                gap_pct_series = (fast_ma - slow_ma).abs() / slow_ma * 100
                pre_cross = gap_pct_series.iloc[-(squeeze_cfg["lookback"] + 1):-1]
                sym_state["squeeze_ok"] = bool((pre_cross <= squeeze_cfg["max_pct"]).all())
            else:
                sym_state["squeeze_ok"] = None

        if crossed_down:
            sym_state["ma_armed"] = False
            sym_state["ma_gap_fired"] = False

        if sym_state["ma_armed"] and not sym_state["ma_gap_fired"]:
            gap_raw = fast_ma.iloc[-1] - slow_ma.iloc[-1]
            gap_pct = abs(gap_raw) / slow_ma.iloc[-1] * 100

            if gap_cfg["mode"] == "Off":
                gap_ok = True
            elif gap_cfg["mode"] == "% Gap":
                gap_ok = gap_pct >= gap_cfg["min_pct"]
            else:  # "Std Dev"
                gap_series = fast_ma - slow_ma
                mean = gap_series.rolling(gap_cfg["stdev_length"]).mean().iloc[-1]
                stdev = gap_series.rolling(gap_cfg["stdev_length"]).std().iloc[-1]
                gap_ok = gap_raw >= (mean + gap_cfg["stdev_multiplier"] * stdev)

            # Volume - REQUIRED when enabled: 15m spike (strict) AND 1h
            # spike (looser) both have to clear their own bar. The 24h
            # floor was already applied before this symbol was ever
            # fetched, so it's looked up here only for display.
            ratio_15m = ratio_1h = None
            if vol_cfg["enabled"]:
                vol = df["volume"]

                baseline_15m = vol.iloc[-(vol_cfg["lookback_15m"] + 1):-1].mean()
                ratio_15m = (vol.iloc[-1] / baseline_15m) if baseline_15m > 0 else 0.0
                ok_15m = ratio_15m >= vol_cfg["multiplier_15m"]

                hour_sums = vol.rolling(candles_per_hour).sum()
                current_hour_vol = hour_sums.iloc[-1]
                baseline_1h = hour_sums.iloc[-(vol_cfg["lookback_1h"] + 1):-1].mean()
                ratio_1h = (current_hour_vol / baseline_1h) if baseline_1h and baseline_1h > 0 else 0.0
                ok_1h = ratio_1h >= vol_cfg["multiplier_1h"]

                volume_ok = ok_15m and ok_1h
            else:
                volume_ok = True

            if gap_ok and volume_ok:
                sym_state["ma_gap_fired"] = True
                detail = f"gap={gap_pct:.3f}%"
                if vol_cfg["enabled"]:
                    vol_24h_m = volume_24h.get(symbol, 0.0) / 1_000_000
                    detail += f", vol15m={ratio_15m:.2f}x, vol1h={ratio_1h:.2f}x, vol24h={vol_24h_m:.1f}M"
                if squeeze_cfg["enabled"]:
                    detail += f", squeeze={'yes' if sym_state.get('squeeze_ok') else 'no'}"
                hits.append({
                    "symbol": symbol,
                    "type": "MA9/20 Cross BUY",
                    "price": close.iloc[-1],
                    "detail": detail,
                })

    state[symbol] = sym_state
    return hits


# ============================================================
# SCALP MODE (new, independent - no shared state with check_symbol)
# ============================================================

def check_symbol_scalp(symbol: str) -> dict | None:
    """Snapshot-style check for the 5m scalp radar. No armed/fired state
    across runs on purpose - this is meant to surface *current* early
    movers each run, then get ranked and trimmed to top_n in run_scan().
    Returns a candidate dict, or None if it fails any filter."""
    cfg = CONFIG["scalp_mode"]

    min_history = max(cfg["dema_length"] * 2, cfg["vol_lookback"] + 1, 50)
    df = get_klines(symbol, interval=cfg["interval"], limit=max(cfg["candle_limit"], min_history + 5))
    if len(df) < min_history:
        return None

    df = df.iloc[:-1]  # drop the still-forming candle
    close = df["close"]
    open_ = df["open"]
    vol = df["volume"]

    # -- 5m volume spike: fire candle vs its own recent baseline --
    baseline = vol.iloc[-(cfg["vol_lookback"] + 1):-1].mean()
    ratio = (vol.iloc[-1] / baseline) if baseline > 0 else 0.0
    if ratio < cfg["vol_multiplier"]:
        return None

    # -- green candle / price up % --
    last_open, last_close = open_.iloc[-1], close.iloc[-1]
    change_pct = (last_close - last_open) / last_open * 100
    if not (last_close > last_open and change_pct >= cfg["min_price_change_pct"]):
        return None

    # -- DEMA200 trend filter (5m) --
    dema_val = dema(close, cfg["dema_length"])
    last_dema = dema_val.iloc[-1]
    dema_pct = (last_close - last_dema) / last_dema * 100
    if dema_pct < cfg["dema_min_pct_above"]:
        return None

    # -- SuperTrend bullish (5m), same atr/multiplier as main config --
    st = supertrend(df, CONFIG["supertrend"]["atr_length"], CONFIG["supertrend"]["multiplier"])
    if not (st["direction"].iloc[-1] < 0):
        return None

    return {
        "symbol": symbol,
        "price": last_close,
        "ratio": ratio,
        "change_pct": change_pct,
        "dema_pct": dema_pct,
    }


def run_scalp_scan(symbols: list[str]) -> None:
    cfg = CONFIG["scalp_mode"]
    print(f"\nScalp mode: scanning {len(symbols)} pairs on {cfg['interval']}...")

    candidates = []
    for i, symbol in enumerate(symbols, 1):
        try:
            hit = check_symbol_scalp(symbol)
            if hit:
                candidates.append(hit)
        except Exception as e:
            print(f"  {symbol}: scalp skipped ({e})")
        time.sleep(CONFIG["request_sleep"])

        if i % 50 == 0:
            print(f"  ...{i}/{len(symbols)} scalp-scanned")

    candidates.sort(key=lambda c: c["ratio"], reverse=True)
    top = candidates[: cfg["top_n"]]

    if not top:
        print("  No scalp setups this run.")
        return

    lines = [f"🚀 **Scalp Setup** (5m vol+price+DEMA200+SuperTrend) - top {len(top)}"]
    for c in top:
        lines.append(
            f"{c['symbol']} @ {c['price']} - vol5m={c['ratio']:.2f}x, "
            f"chg={c['change_pct']:.2f}%, DEMA200 +{c['dema_pct']:.2f}%"
        )
    msg = "\n".join(lines)
    print(msg)
    send_discord(msg)


# ============================================================
# Main scan loop
# ============================================================

def run_scan() -> None:
    print(f"Fetching Binance {CONFIG['quote_asset']} pairs...")
    symbols = get_usdt_symbols()

    volume_24h: dict[str, float] = {}
    vol_cfg = CONFIG["volume_spike"]
    if vol_cfg["enabled"]:
        print("Fetching 24h volume for the liquidity floor (1 call, all symbols)...")
        volume_24h = get_24h_volumes()
        before = len(symbols)
        symbols = [s for s in symbols if volume_24h.get(s, 0.0) >= vol_cfg["min_24h_volume_usdt"]]
        floor_m = vol_cfg["min_24h_volume_usdt"] / 1_000_000
        print(f"  {before} pairs -> {len(symbols)} pairs clear the {floor_m:.1f}M 24h floor")

    print(f"Scanning {len(symbols)} pairs on {CONFIG['interval']} timeframe...\n")

    state = load_state()
    all_hits = []

    for i, symbol in enumerate(symbols, 1):
        try:
            hits = check_symbol(symbol, state, volume_24h)
            for h in hits:
                all_hits.append(h)
                msg = f"[{h['type']}] {h['symbol']} @ {h['price']} ({h['detail']})"
                print(msg)
                send_discord(msg)
        except Exception as e:
            print(f"  {symbol}: skipped ({e})")
        time.sleep(CONFIG["request_sleep"])

        if i % 50 == 0:
            print(f"  ...{i}/{len(symbols)} scanned")

    save_state(state)
    print(f"\nDone. {len(all_hits)} fresh signal(s) found.")

    # ---- Scalp mode: fully separate pass, only runs if switched on ----
    if CONFIG["scalp_mode"]["enabled"]:
        run_scalp_scan(symbols)


if __name__ == "__main__":
    run_scan()