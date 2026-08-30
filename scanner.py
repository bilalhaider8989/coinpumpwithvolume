"""
Free Binance scanner - configurable DEMA200 + SuperTrend + MA9/20 cross,
with Discord notifications. Designed to run on a schedule via GitHub
Actions, so it works even when your own laptop is off.
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
    # REQUIRED when enabled: if the fire candle doesn't have a volume spike,
    # the alert is suppressed entirely (not just noted in the message).
    "volume_spike": {
        "enabled": True,
        "lookback": 10,       # candles used to build the average-volume baseline
        "multiplier": 1.2,    # fire candle's volume must be >= multiplier x baseline
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

    # ---- 5. Discord notifications (free, no bot needed) ----
    "discord_webhook_url": os.environ.get("DISCORD_WEBHOOK_URL", ""),
}


# ============================================================
# Binance data fetching
# ============================================================

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


def get_klines(symbol: str) -> pd.DataFrame:
    r = requests.get(
        f"{BINANCE_BASE}/api/v3/klines",
        params={
            "symbol": symbol,
            "interval": CONFIG["interval"],
            "limit": CONFIG["candle_limit"],
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
# Per-symbol evaluation
# ============================================================

def check_symbol(symbol: str, state: dict) -> list[dict]:
    """Returns a list of hit dicts (can contain 0, 1, or 2 signals per symbol)."""
    dema_cfg = CONFIG["dema"]
    ma_cfg = CONFIG["ma_cross"]
    gap_cfg = CONFIG["ma_gap"]
    vol_cfg = CONFIG["volume_spike"]
    squeeze_cfg = CONFIG["squeeze"]

    min_history = max(
        dema_cfg["length"] * 2 if dema_cfg["enabled"] else 0,
        ma_cfg["slow_length"] + gap_cfg["stdev_length"] if ma_cfg["enabled"] else 0,
        ma_cfg["slow_length"] + squeeze_cfg["lookback"] if ma_cfg["enabled"] and squeeze_cfg["enabled"] else 0,
        vol_cfg["lookback"] + 1 if vol_cfg["enabled"] else 0,
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

            # Volume spike - REQUIRED when enabled (gates the alert, same as gap_ok)
            vol_ratio = None
            if vol_cfg["enabled"]:
                vol = df["volume"]
                baseline = vol.iloc[-(vol_cfg["lookback"] + 1):-1].mean()
                vol_ratio = (vol.iloc[-1] / baseline) if baseline > 0 else 0.0
                volume_ok = vol_ratio >= vol_cfg["multiplier"]
            else:
                volume_ok = True

            if gap_ok and volume_ok:
                sym_state["ma_gap_fired"] = True
                detail = f"gap={gap_pct:.3f}%"
                if vol_cfg["enabled"]:
                    detail += f", vol={vol_ratio:.2f}x avg"
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
# Main scan loop
# ============================================================

def run_scan() -> None:
    print(f"Fetching Binance {CONFIG['quote_asset']} pairs...")
    symbols = get_usdt_symbols()
    print(f"Scanning {len(symbols)} pairs on {CONFIG['interval']} timeframe...\n")

    state = load_state()
    all_hits = []

    for i, symbol in enumerate(symbols, 1):
        try:
            hits = check_symbol(symbol, state)
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


if __name__ == "__main__":
    run_scan()