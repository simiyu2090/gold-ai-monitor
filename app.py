from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
import sqlite3
import json

from liquidity_store import (
    initialize_database,
    load_recent_snapshots,
    save_snapshot,
)

initialize_database()
# --------------------------------------------------
# 1. Application settings
# --------------------------------------------------
BASE_URL = "https://api.bybit.com"
BYBIT_HOSTS = (
    "https://api.bybit.com",
    "https://api.bytick.com",
)
SYMBOL = "XAUTUSDT"
CATEGORY = "linear"
INTERVAL = "5"
CANDLE_LIMIT = 300
BINANCE_FAPI = "https://fapi.binance.com"
BINANCE_GOLD_SYMBOL = "PAXGUSDT"
REQUEST_HEADERS = {
    "Accept": "application/json",
    "User-Agent": (
        "GoldAIMonitor/1.0 "
        "(educational dashboard; public market data only)"
    ),
}
BINANCE_INTERVALS = {
    "1": "1m",
    "3": "3m",
    "5": "5m",
    "15": "15m",
    "30": "30m",
    "60": "1h",
    "120": "2h",
    "240": "4h",
    "360": "6h",
    "720": "12h",
    "D": "1d",
    "W": "1w",
    "M": "1M",
}
_active_vendor = "bybit"

st.set_page_config(
    page_title="Gold AI Market Monitor",
    page_icon="🟡",
    layout="wide",
)


st.title("Gold AI Market Monitor")
st.caption("Bybit | XAUTUSDT Perpetual | Market analysis only")

def binance_market_result(endpoint, params):
    """Return Bybit-shaped public data from Binance USDT-M gold futures.

    Streamlit Cloud often uses US/cloud IPs that Bybit's CDN blocks with
    HTTP 403. PAXGUSDT is a nearby tokenized-gold perpetual used only as
    a fallback so the hosted dashboard can still load.
    """
    params = params or {}
    symbol = BINANCE_GOLD_SYMBOL

    if endpoint == "/v5/market/instruments-info":
        response = requests.get(
            f"{BINANCE_FAPI}/fapi/v1/exchangeInfo",
            headers=REQUEST_HEADERS,
            timeout=15,
        )
        response.raise_for_status()
        match = next(
            (
                item for item in response.json().get("symbols", [])
                if item.get("symbol") == symbol
            ),
            None,
        )
        if not match:
            raise RuntimeError(
                f"{symbol} was not found on Binance USDT-M futures."
            )

        filters = {
            item.get("filterType"): item
            for item in match.get("filters", [])
        }
        price = filters.get("PRICE_FILTER", {})
        lot = filters.get("LOT_SIZE", {})
        status = (
            "Trading"
            if match.get("status") == "TRADING"
            else match.get("status")
        )
        contract_type = (
            "LinearPerpetual"
            if match.get("contractType") == "PERPETUAL"
            else match.get("contractType")
        )
        return {
            "list": [{
                "symbol": match.get("symbol"),
                "contractType": contract_type,
                "status": status,
                "baseCoin": match.get("baseAsset"),
                "quoteCoin": match.get("quoteAsset"),
                "settleCoin": match.get("marginAsset") or match.get("quoteAsset"),
                "priceFilter": {"tickSize": price.get("tickSize")},
                "lotSizeFilter": {"qtyStep": lot.get("stepSize")},
            }]
        }

    if endpoint == "/v5/market/tickers":
        response = requests.get(
            f"{BINANCE_FAPI}/fapi/v1/ticker/24hr",
            params={"symbol": symbol},
            headers=REQUEST_HEADERS,
            timeout=15,
        )
        response.raise_for_status()
        ticker = response.json()
        return {
            "list": [{
                "lastPrice": ticker.get("lastPrice"),
            }]
        }

    if endpoint == "/v5/market/kline":
        interval = BINANCE_INTERVALS.get(
            str(params.get("interval", INTERVAL)),
            "5m",
        )
        response = requests.get(
            f"{BINANCE_FAPI}/fapi/v1/klines",
            params={
                "symbol": symbol,
                "interval": interval,
                "limit": params.get("limit", CANDLE_LIMIT),
            },
            headers=REQUEST_HEADERS,
            timeout=15,
        )
        response.raise_for_status()
        rows = []
        for candle in reversed(response.json()):
            rows.append([
                candle[0],
                candle[1],
                candle[2],
                candle[3],
                candle[4],
                candle[5],
                candle[7],
            ])
        return {"list": rows}

    if endpoint == "/v5/market/orderbook":
        response = requests.get(
            f"{BINANCE_FAPI}/fapi/v1/depth",
            params={"symbol": symbol, "limit": 100},
            headers=REQUEST_HEADERS,
            timeout=15,
        )
        response.raise_for_status()
        book = response.json()
        return {
            "b": book.get("bids", []),
            "a": book.get("asks", []),
        }

    raise RuntimeError(
        f"No Binance fallback is defined for {endpoint}."
    )


# Public market-data requests need no API key.
@st.cache_data(ttl=2, show_spinner=False)
def api_get(endpoint, params):
    global _active_vendor
    params = dict(params or {})
    last_error = None

    for host in BYBIT_HOSTS:
        try:
            response = requests.get(
                f"{host}{endpoint}",
                params=params,
                headers=REQUEST_HEADERS,
                timeout=15,
            )
            if response.status_code == 403:
                last_error = RuntimeError(
                    f"403 Forbidden from {host}"
                )
                continue

            response.raise_for_status()
            data = response.json()

            if data.get("retCode") != 0:
                raise RuntimeError(
                    f"Bybit error {data.get('retCode')}: "
                    f"{data.get('retMsg')}"
                )

            _active_vendor = "bybit"
            return data["result"]
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            last_error = exc
            continue

    try:
        result = binance_market_result(endpoint, params)
        _active_vendor = "binance"
        return result
    except Exception as exc:
        raise RuntimeError(
            "Bybit blocked this host with HTTP 403 (common on "
            "Streamlit Cloud / US IPs), and the Binance gold "
            f"fallback also failed: {exc}"
        ) from last_error


def get_instrument():
    result = api_get(
        "/v5/market/instruments-info",
        {"category": CATEGORY, "symbol": SYMBOL},
    )

    instruments = result.get("list", []) or []
    if not instruments:
        raise RuntimeError(
            f"{SYMBOL} was not found in Bybit's "
            "linear instruments list."
        )

    instrument = instruments[0] or {}
    if instrument.get("contractType") != "LinearPerpetual":
        raise RuntimeError(
            f"Wrong contract type: "
            f"{instrument.get('contractType')}"
        )

    if instrument.get("status") != "Trading":
        raise RuntimeError(
            f"Contract is not trading. "
            f"Status: {instrument.get('status')}"
        )

    if (
        instrument.get("quoteCoin") != "USDT"
        or instrument.get("settleCoin") != "USDT"
    ):
        raise RuntimeError(
            "The contract's quote or settlement currency "
            "is not USDT."
        )

    return instrument


def get_ticker():
    result = api_get(
        "/v5/market/tickers",
        {"category": CATEGORY, "symbol": SYMBOL},
    )
    rows = result.get("list", []) or []

    if not rows:
        raise RuntimeError("No ticker data returned.")

    return rows[0]


def get_candles():
    result = api_get(
        "/v5/market/kline",
        {
            "category": CATEGORY,
            "symbol": SYMBOL,
            "interval": INTERVAL,
            "limit": CANDLE_LIMIT,
        },
    )

    rows = result.get("list", []) or []
    if not rows:
        raise RuntimeError("No candle data returned.")

    # Bybit returns candles newest first.
    df = pd.DataFrame(
        rows,
        columns=[
            "start", "open", "high", "low",
            "close", "volume", "turnover",
        ],
    )

    df["time"] = pd.to_datetime(
        pd.to_numeric(df["start"], errors="coerce"),
        unit="ms",
        utc=True,
    )

    for col in [
        "open", "high", "low", "close",
        "volume", "turnover",
    ]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = (
        df.dropna(subset=["close", "time"])
        .sort_values("time")
        .drop_duplicates(subset=["time"])
        .reset_index(drop=True)
    )

    # Exclude any candle whose five-minute interval
    # has not finished yet.
    now = pd.Timestamp.now(tz="UTC")
    candle_end = df["time"] + pd.Timedelta(minutes=5)
    df = df.loc[candle_end <= now].copy()

    return df.reset_index(drop=True)


def get_orderbook():
    """Fetch a public snapshot of the Bybit linear order book."""
    result = api_get(
        "/v5/market/orderbook",
        {
            "category": CATEGORY,
            "symbol": SYMBOL,
            "limit": 200,
        },
    )

    bids = result.get("b", []) or []
    asks = result.get("a", []) or []

    if not bids or not asks:
        raise RuntimeError("Order book has no bids or asks.")

    bids_df = pd.DataFrame(bids, columns=["price", "size"])
    asks_df = pd.DataFrame(asks, columns=["price", "size"])

    for df in (bids_df, asks_df):
        df["price"] = pd.to_numeric(df["price"], errors="coerce")
        df["size"] = pd.to_numeric(df["size"], errors="coerce")
        df.dropna(subset=["price", "size"], inplace=True)

    if bids_df.empty or asks_df.empty:
        raise RuntimeError("Order-book snapshot contains invalid levels.")

    best_bid = float(bids_df["price"].max())
    best_ask = float(asks_df["price"].min())

    if best_bid >= best_ask:
        raise RuntimeError("Order-book spread is invalid or crossed.")

    return bids_df, asks_df, (best_bid + best_ask) / 2


def get_timeframe_trend(interval):
    """Calculate EMA20/EMA50 trend using completed candles only."""
    try:
        result = api_get(
            "/v5/market/kline",
            {
                "category": CATEGORY,
                "symbol": SYMBOL,
                "interval": interval,
                "limit": 200,
            },
        )

        rows = result.get("list", [])

        if len(rows) < 55:
            return {
                "timeframe": interval,
                "close": None,
                "ema20": None,
                "ema50": None,
                "bias": "Insufficient data",
            }

        df = pd.DataFrame(
            rows,
            columns=[
                "start_time", "open", "high", "low",
                "close", "volume", "turnover"
            ],
        )

        df["start_time"] = pd.to_numeric(
            df["start_time"], errors="coerce"
        )
        df["close"] = pd.to_numeric(
            df["close"], errors="coerce"
        )

        df = (
            df.dropna(subset=["start_time", "close"])
            .sort_values("start_time")
            .reset_index(drop=True)
        )

        # Exclude the newest candle because it may still be forming.
        df = df.iloc[:-1].copy()

        if len(df) < 50:
            raise ValueError("Not enough completed candles.")

        df["ema20"] = df["close"].ewm(
            span=20, adjust=False
        ).mean()

        df["ema50"] = df["close"].ewm(
            span=50, adjust=False
        ).mean()

        last = df.iloc[-1]

        if last["ema20"] > last["ema50"]:
            bias = "Bullish"
        elif last["ema20"] < last["ema50"]:
            bias = "Bearish"
        else:
            bias = "Neutral"

        return {
            "timeframe": interval,
            "close": float(last["close"]),
            "ema20": float(last["ema20"]),
            "ema50": float(last["ema50"]),
            "bias": bias,
        }

    except Exception as exc:
        return {
            "timeframe": interval,
            "close": None,
            "ema20": None,
            "ema50": None,
            "bias": f"Unavailable: {exc}",
        }


def detect_candlestick_patterns(interval):
    """Detect selected patterns on the latest completed candle."""
    result = api_get(
        "/v5/market/kline",
        {
            "category": CATEGORY,
            "symbol": SYMBOL,
            "interval": interval,
            "limit": 100,
        },
    )

    rows = result.get("list", [])

    columns = [
        "start_time", "open", "high", "low",
        "close", "volume", "turnover"
    ]

    df = pd.DataFrame(rows, columns=columns)

    if df.empty:
        return {
            "timeframe": interval,
            "close": None,
            "patterns": ["No candle data"],
        }

    for col in ["open", "high", "low", "close", "start_time"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    df = (
        df.dropna(subset=["open", "high", "low", "close", "start_time"])
        .sort_values("start_time")
        .reset_index(drop=True)
    )

    # The newest candle may not be complete.
    df = df.iloc[:-1].copy()

    if len(df) < 22:
        return {
            "timeframe": interval,
            "close": None,
            "patterns": ["Insufficient completed candles"],
        }

    current = df.iloc[-1]
    previous = df.iloc[-2]

    open_price = float(current["open"])
    high = float(current["high"])
    low = float(current["low"])
    close = float(current["close"])

    previous_open = float(previous["open"])
    previous_close = float(previous["close"])

    body = abs(close - open_price)
    candle_range = high - low

    upper_wick = high - max(open_price, close)
    lower_wick = min(open_price, close) - low

    patterns = []

    # Engulfing patterns compare candle bodies.
    if (
        previous_close < previous_open
        and close > open_price
        and open_price <= previous_close
        and close >= previous_open
    ):
        patterns.append("Bullish engulfing")

    if (
        previous_close > previous_open
        and close < open_price
        and open_price >= previous_close
        and close <= previous_open
    ):
        patterns.append("Bearish engulfing")

    # Pin bars: long wick relative to the body.
    if candle_range > 0 and body > 0:
        if (
            lower_wick >= 2 * body
            and upper_wick <= body
            and close >= low + 0.6 * candle_range
        ):
            patterns.append("Bullish pin bar")

        if (
            upper_wick >= 2 * body
            and lower_wick <= body
            and close <= low + 0.4 * candle_range
        ):
            patterns.append("Bearish pin bar")

    # Compare the candle close with the preceding 20-candle range.
    prior_20 = df.iloc[-21:-1]
    prior_high = float(prior_20["high"].max())
    prior_low = float(prior_20["low"].min())

    if close > prior_high:
        patterns.append("Bullish breakout close")

    if close < prior_low:
        patterns.append("Bearish breakout close")

    if not patterns:
        patterns.append("No selected pattern detected")

    return {
        "timeframe": interval,
        "close": close,
        "patterns": patterns,
    }


def liquidity_band_width(tick_size):
    """Return a stable aggregation width of 50 ticks.

    The width depends only on the contract tick, so a resting order
    stays on the same heatmap row from one snapshot to the next.
    """
    tick = Decimal(str(tick_size))
    if tick <= 0:
        tick = Decimal("0.01")
    return tick * 50


def aggregate_liquidity(levels, tick_size):
    """Sum order size onto the nearest 50-tick price band."""
    tick = Decimal(str(tick_size))
    if tick <= 0:
        tick = Decimal("0.01")

    band_ticks = 50
    scale = int((Decimal(1) / tick).to_integral_value())
    band_width = tick * band_ticks
    tick_index = (levels["price"] * scale).round().astype("int64")
    band_units = (tick_index + band_ticks // 2) // band_ticks
    price_band = band_units.map(
        lambda unit: float(Decimal(int(unit)) * band_width)
    )

    return (
        levels.assign(price_band=price_band)
        .groupby("price_band", sort=True)["size"]
        .sum()
    )


def detect_liquidity_zones(bids, asks):
    """Flag unusually large displayed orders on each side."""
    zones = []

    for side, book in [("BID", bids), ("ASK", asks)]:
        if book.empty:
            continue

        threshold = book["size"].quantile(0.95)

        large_levels = book.loc[
            book["size"] >= threshold
        ].sort_values("size", ascending=False)

        for _, row in large_levels.head(10).iterrows():
            zones.append({
                "side": side,
                "price": float(row["price"]),
                "quantity": float(row["size"]),
                "threshold": float(threshold),
            })

    return pd.DataFrame(
        zones,
        columns=["side", "price", "quantity", "threshold"],
    )


def track_persistent_zones(history, min_observations=3):
    """
    Find price levels with unusually large displayed liquidity
    in multiple saved snapshots.

    This is a heuristic, not a trading signal.
    """
    observations = {}

    for sample in history:
        levels = sample.get("levels", {})
        quantities = [
            abs(float(q))
            for q in levels.values()
            if float(q) != 0
        ]

        if not quantities:
            continue

        threshold = pd.Series(quantities).quantile(0.95)

        for price, quantity in levels.items():
            quantity = float(quantity)

            if abs(quantity) < threshold:
                continue

            # Round to a stable $1 price bucket for tracking.
            # This is a display heuristic, not an exchange tick size.
            bucket = round(float(price))

            if bucket not in observations:
                observations[bucket] = {
                    "observations": 0,
                    "bid_seen": 0,
                    "ask_seen": 0,
                    "latest_quantity": 0.0,
                    "last_seen": None,
                }

            zone = observations[bucket]
            zone["observations"] += 1
            zone["latest_quantity"] = quantity
            zone["last_seen"] = sample.get("time")

            if quantity > 0:
                zone["bid_seen"] += 1
            else:
                zone["ask_seen"] += 1

    rows = []

    for price, zone in observations.items():
        count = zone["observations"]

        if count < min_observations:
            continue

        side = (
            "BID"
            if zone["bid_seen"] > zone["ask_seen"]
            else "ASK"
            if zone["ask_seen"] > zone["bid_seen"]
            else "MIXED"
        )

        rows.append({
            "price": price,
            "side": side,
            "observations": count,
            "latest_size": abs(zone["latest_quantity"]),
            "last_seen": zone["last_seen"],
            "label": "Repeatedly observed",
        })

    result = pd.DataFrame(rows)

    if not result.empty:
        result = result.sort_values(
            ["observations", "latest_size"],
            ascending=False,
        )

    return result


def assess_confluence(trend_df, pattern_df, persistent_zones):
    """
    Combine trend, candle-pattern, and liquidity observations.
    This is a transparent heuristic, not a predictive model.
    """
    result = {
        "status": "INSUFFICIENT DATA",
        "bullish_points": 0,
        "bearish_points": 0,
        "reasons": [],
    }

    if trend_df.empty or pattern_df.empty:
        result["reasons"].append("Trend or pattern data is missing.")
        return result

    trends = dict(zip(trend_df["Timeframe"], trend_df["Trend"]))
    patterns = dict(
        zip(pattern_df["Timeframe"], pattern_df["Detected patterns"])
    )

    required = ["5-minute", "15-minute", "30-minute",
                "1-hour", "4-hour", "1-day"]

    if any(t not in trends for t in required):
        result["reasons"].append("Not all six timeframes are present.")
        return result

    if any(
        trends[t] not in ("Bullish", "Bearish", "Neutral")
        for t in required
    ):
        result["reasons"].append("One or more timeframe trends are unavailable.")
        return result

    bullish = 0
    bearish = 0
    reasons = []

    # Higher timeframes carry more weight in this heuristic.
    weights = {
        "1-day": 3,
        "4-hour": 2,
        "1-hour": 2,
        "30-minute": 1,
        "15-minute": 1,
        "5-minute": 1,
    }

    for timeframe, weight in weights.items():
        bias = trends[timeframe]

        if bias == "Bullish":
            bullish += weight
            reasons.append(f"{timeframe}: bullish EMA alignment.")
        elif bias == "Bearish":
            bearish += weight
            reasons.append(f"{timeframe}: bearish EMA alignment.")

    # Only short-term candle patterns contribute here.
    for timeframe in ["5-minute", "15-minute"]:
        text = str(patterns.get(timeframe, ""))

        if "Unavailable" in text or "Insufficient" in text:
            result["reasons"].append(
                f"{timeframe}: pattern data is unavailable."
            )
            return result

        bullish_pattern = any(
            p in text for p in [
                "Bullish engulfing",
                "Bullish pin bar",
                "Bullish breakout close",
            ]
        )

        bearish_pattern = any(
            p in text for p in [
                "Bearish engulfing",
                "Bearish pin bar",
                "Bearish breakout close",
            ]
        )

        if bullish_pattern and not bearish_pattern:
            bullish += 1
            reasons.append(f"{timeframe}: bullish candle pattern detected.")
        elif bearish_pattern and not bullish_pattern:
            bearish += 1
            reasons.append(f"{timeframe}: bearish candle pattern detected.")
        elif bullish_pattern and bearish_pattern:
            reasons.append(
                f"{timeframe}: conflicting candle patterns detected."
            )

    result["bullish_points"] = bullish
    result["bearish_points"] = bearish
    result["reasons"] = reasons

    # A simple evidence label, not a calibrated probability.
    if bullish >= bearish + 4:
        result["status"] = "BULLISH EVIDENCE"
    elif bearish >= bullish + 4:
        result["status"] = "BEARISH EVIDENCE"
    else:
        result["status"] = "MIXED EVIDENCE"

    # Liquidity is contextual; it does not add directional points.
    if persistent_zones is None or persistent_zones.empty:
        result["reasons"].append(
            "No persistent liquidity zones available for context."
        )
    else:
        result["reasons"].append(
            f"{len(persistent_zones)} repeatedly observed liquidity zones "
            "are available for review; these do not confirm direction."
        )

    return result


def generate_market_briefing(
    trend_df,
    pattern_df,
    assessment,
    persistent_zones,
):
    lines = [
        "## Gold market briefing",
        "",
        "**Overall assessment:** "
        + str(assessment.get("status", "Unavailable")),
        "",
    ]

    # 1. Summarize timeframe trends
    lines.append("### 1. Multi-timeframe trend")

    if trend_df is None or trend_df.empty:
        lines.append("- Trend data is unavailable.")
    else:
        timeframe_order = [
            "1-day",
            "4-hour",
            "1-hour",
            "30-minute",
            "15-minute",
            "5-minute",
        ]

        for timeframe in timeframe_order:
            rows = trend_df[
                trend_df["Timeframe"] == timeframe
            ]

            if rows.empty:
                lines.append(
                    f"- {timeframe}: data unavailable."
                )
                continue

            bias = str(rows.iloc[0]["Trend"])

            if bias == "Bullish":
                explanation = (
                    "EMA 20 is above EMA 50; "
                    "bullish trend evidence."
                )
            elif bias == "Bearish":
                explanation = (
                    "EMA 20 is below EMA 50; "
                    "bearish trend evidence."
                )
            elif bias == "Neutral":
                explanation = (
                    "No clear EMA trend alignment."
                )
            else:
                explanation = "Trend classification unavailable."

            lines.append(
                f"- **{timeframe}:** {bias}. {explanation}"
            )

    # 2. Summarize short-term candlestick patterns
    lines.extend([
        "",
        "### 2. Short-term price action",
    ])

    if pattern_df is None or pattern_df.empty:
        lines.append("- Candlestick pattern data is unavailable.")
    else:
        for timeframe in ["5-minute", "15-minute"]:
            rows = pattern_df[
                pattern_df["Timeframe"] == timeframe
            ]

            if rows.empty:
                lines.append(
                    f"- {timeframe}: no data available."
                )
                continue

            patterns = str(
                rows.iloc[0]["Detected patterns"]
            )

            if (
                "Unavailable" in patterns
                or "Insufficient" in patterns
            ):
                explanation = "Pattern data needs checking."
            elif "No selected pattern detected" in patterns:
                explanation = (
                    "No selected pattern was detected."
                )
            else:
                explanation = patterns

            lines.append(
                f"- **{timeframe}:** {explanation}"
            )

    # 3. Report confluence scores
    lines.extend([
        "",
        "### 3. Evidence summary",
        (
            "- Bullish evidence points: "
            f"{assessment.get('bullish_points', 0)}"
        ),
        (
            "- Bearish evidence points: "
            f"{assessment.get('bearish_points', 0)}"
        ),
    ])

    reasons = assessment.get("reasons", [])

    if reasons:
        lines.append("")
        lines.append("**Assessment notes:**")
        for reason in reasons:
            lines.append(f"- {reason}")

    # 4. Report liquidity-history availability
    lines.extend([
        "",
        "### 4. Liquidity context",
    ])

    if persistent_zones is None:
        lines.append(
            "- Persistent liquidity-zone data is unavailable."
        )
    elif len(persistent_zones) == 0:
        lines.append(
            "- No persistent zones currently meet the "
            "configured detection criteria."
        )
    else:
        lines.append(
            f"- {len(persistent_zones)} persistent zone(s) "
            "are available for review."
        )

    lines.extend([
        "",
        "### 5. Interpretation and caution",
    ])

    status = assessment.get("status", "")

    if status == "BULLISH EVIDENCE":
        lines.append(
            "- The configured rules find more bullish "
            "than bearish evidence by the required margin. "
            "This is not a buy signal by itself."
        )
    elif status == "BEARISH EVIDENCE":
        lines.append(
            "- The configured rules find more bearish "
            "than bullish evidence by the required margin. "
            "This is not a sell signal by itself."
        )
    elif status == "MIXED EVIDENCE":
        lines.append(
            "- Evidence is mixed. Avoid treating the "
            "assessment as a clear directional advantage."
        )
    else:
        lines.append(
            "- Data is insufficient for a reliable "
            "combined assessment. Check missing inputs."
        )

    lines.append(
        "- Order-book liquidity can disappear or move. "
        "Wait for independent confirmation and apply "
        "your risk limits before making any trading decision."
    )

    return "\n".join(lines)


def add_indicators(df):
    df = df.copy()

    # Exponential moving averages
    df["EMA20"] = df["close"].ewm(span=20, adjust=False).mean()
    df["EMA50"] = df["close"].ewm(span=50, adjust=False).mean()

    # RSI, using Wilder-style exponential smoothing
    change = df["close"].diff()
    gains = change.clip(lower=0)
    losses = -change.clip(upper=0)

    avg_gain = gains.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()
    avg_loss = losses.ewm(alpha=1 / 14, min_periods=14, adjust=False).mean()

    # A period with losses uses the usual RS ratio. A run of gains
    # and no losses is RSI 100; a flat period is RSI 50. Leaving RS
    # at 0 in those cases would print RSI 0.
    rs = pd.Series(float("nan"), index=df.index, dtype="float64")
    has_loss = avg_loss > 0
    rs.loc[has_loss] = avg_gain.loc[has_loss] / avg_loss.loc[has_loss]
    df["RSI14"] = 100 - (100 / (1 + rs))
    df.loc[(avg_loss == 0) & (avg_gain > 0), "RSI14"] = 100.0
    df.loc[(avg_loss == 0) & (avg_gain == 0), "RSI14"] = 50.0

    # Average True Range
    previous_close = df["close"].shift(1)

    true_range = pd.concat(
        [
            df["high"] - df["low"],
            (df["high"] - previous_close).abs(),
            (df["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)

    df["ATR14"] = true_range.ewm(
        alpha=1 / 14, min_periods=14, adjust=False
    ).mean()

    return df


def classify_setup(df):
    if len(df) < 60:
        return (
            "INSUFFICIENT DATA",
            "Wait until enough completed candles are available.",
        )

    last = df.iloc[-1]

    values = [
        last["EMA20"], last["EMA50"],
        last["RSI14"], last["ATR14"],
    ]

    if pd.isna(values).any():
        return "INSUFFICIENT DATA", "Indicators are not ready."

    bullish = (
        last["close"] > last["EMA20"]
        and last["EMA20"] > last["EMA50"]
        and 50 <= last["RSI14"] < 70
    )

    bearish = (
        last["close"] < last["EMA20"]
        and last["EMA20"] < last["EMA50"]
        and 30 < last["RSI14"] <= 50
    )

    if bullish:
        return (
            "POTENTIAL BULLISH SETUP",
            "Price > EMA20 > EMA50; RSI is between 50 and 70.",
        )

    if bearish:
        return (
            "POTENTIAL BEARISH SETUP",
            "Price < EMA20 < EMA50; RSI is between 30 and 50.",
        )

    return (
        "NO SETUP",
        "The current rules do not show a qualifying setup.",
    )


@st.fragment(run_every=timedelta(seconds=5))
def render_market():
    # --------------------------------------------------
    # 2. User controls
    # --------------------------------------------------
    left, right = st.columns([3, 1])

    with left:
        st.write("**Instrument:** XAUTUSDT or Binance PAXGUSDT linear perpetual")
        st.caption(
            "Last refresh: "
            + datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
        )

    with right:
        refresh = st.button("Refresh market data", type="primary")

    if refresh:
        api_get.clear()

    # --------------------------------------------------
    # 3. Fetch, validate and analyze
    # --------------------------------------------------
    try:
        with st.spinner("Connecting to market data..."):
            instrument = get_instrument()
            ticker = get_ticker()
            candles = add_indicators(get_candles())

        if _active_vendor == "binance":
            st.warning(
                "Bybit returned HTTP 403 from this host, so the dashboard "
                "is using Binance USDT-M **PAXGUSDT** public data instead. "
                "Bybit blocks many US and cloud IPs. Your Windows copy can "
                "still use Bybit XAUTUSDT when the local IP is allowed."
            )

        if len(candles) < 60:
            st.warning(
                f"Only {len(candles)} completed candles are available. "
                "More history is needed for reliable indicator warm-up."
            )

        if candles.empty:
            st.error("No completed candles are available yet.")
            st.stop()

        latest = candles.iloc[-1]
        price = float(ticker["lastPrice"])

        st.success(
            f"Contract verified: {instrument['symbol']} | "
            f"{instrument['contractType']} | "
            f"{instrument['status']}"
        )

        st.caption(
            "Latest completed candle: "
            + latest["time"].strftime("%Y-%m-%d %H:%M UTC")
        )

        m1, m2, m3, m4 = st.columns(4)

        m1.metric("Latest market price", f"{price:,.4f} USDT")
        m2.metric("5m candle close", f"{latest['close']:,.4f}")
        m3.metric("RSI (14)", f"{latest['RSI14']:.2f}")
        m4.metric("ATR (14)", f"{latest['ATR14']:.4f}")

        # Candlestick chart
        chart = go.Figure()

        chart.add_trace(
            go.Candlestick(
                x=candles["time"],
                open=candles["open"],
                high=candles["high"],
                low=candles["low"],
                close=candles["close"],
                name="XAUTUSDT",
            )
        )

        chart.add_trace(
            go.Scatter(
                x=candles["time"],
                y=candles["EMA20"],
                name="EMA 20",
                mode="lines",
            )
        )

        chart.add_trace(
            go.Scatter(
                x=candles["time"],
                y=candles["EMA50"],
                name="EMA 50",
                mode="lines",
            )
        )

        chart.update_layout(
            title="XAUTUSDT — 5-minute candles",
            xaxis_title="Time (UTC)",
            yaxis_title="Price (USDT)",
            height=600,
            xaxis_rangeslider_visible=False,
            legend_orientation="h",
        )

        st.plotly_chart(chart, width="stretch")

        # RSI chart
        rsi_chart = go.Figure()
        rsi_chart.add_trace(
            go.Scatter(
                x=candles["time"],
                y=candles["RSI14"],
                name="RSI 14",
                mode="lines",
            )
        )

        rsi_chart.add_hline(y=70, line_dash="dash")
        rsi_chart.add_hline(y=30, line_dash="dash")
        rsi_chart.update_layout(
            title="Relative Strength Index",
            yaxis_title="RSI",
            yaxis_range=[0, 100],
            height=300,
        )

        st.plotly_chart(rsi_chart, width="stretch")

        st.subheader("Multi-timeframe trend confirmation")

        timeframe_settings = [
            ("5-minute", "5"),
            ("15-minute", "15"),
            ("30-minute", "30"),
            ("1-hour", "60"),
            ("4-hour", "240"),
            ("1-day", "D"),
        ]

        trend_rows = []

        for label, interval in timeframe_settings:
            trend = get_timeframe_trend(interval)

            trend_rows.append({
                "Timeframe": label,
                "Last closed price": trend["close"],
                "EMA 20": trend["ema20"],
                "EMA 50": trend["ema50"],
                "Trend": trend["bias"],
            })

        trend_df = pd.DataFrame(trend_rows)

        st.dataframe(
            trend_df,
            use_container_width=True,
            hide_index=True,
        )

        st.subheader("Higher-timeframe market bias")

        higher_timeframes = trend_df[
            trend_df["Timeframe"].isin(["4-hour", "1-day"])
        ]

        for _, row in higher_timeframes.iterrows():
            st.write(
                f"**{row['Timeframe']}:** {row['Trend']}"
            )

        higher_biases = higher_timeframes["Trend"].tolist()

        if all(bias == "Bullish" for bias in higher_biases):
            st.info(
                "4H and daily trends are aligned bullish by the EMA rule. "
                "This is context, not a buy signal."
            )
        elif all(bias == "Bearish" for bias in higher_biases):
            st.info(
                "4H and daily trends are aligned bearish by the EMA rule. "
                "This is context, not a sell signal."
            )
        else:
            st.info(
                "The 4H and daily trends disagree or data is unavailable. "
                "Treat the higher-timeframe bias as mixed or unconfirmed."
            )

        valid_trends = trend_df[
            trend_df["Trend"].isin(["Bullish", "Bearish"])
        ]

        bullish_count = (
            valid_trends["Trend"] == "Bullish"
        ).sum()

        bearish_count = (
            valid_trends["Trend"] == "Bearish"
        ).sum()

        n_timeframes = len(timeframe_settings)

        st.write(
            f"Bullish timeframes: {bullish_count}/6 | "
            f"Bearish timeframes: {bearish_count}/6"
        )

        if len(valid_trends) < len(timeframe_settings):
            st.info(
                "Trend confirmation is incomplete. Check data availability "
                "before interpreting the result."
            )
        elif bullish_count == n_timeframes:
            st.success(
                "All six timeframes are bullish by the EMA rule. "
                "This is trend alignment, not a buy signal."
            )
        elif bearish_count == n_timeframes:
            st.warning(
                "All six timeframes are bearish by the EMA rule. "
                "This is trend alignment, not a sell signal."
            )
        else:
            st.info(
                "Timeframes disagree. The market may be mixed or transitioning."
            )

        st.subheader("Candlestick patterns and price action")

        pattern_rows = []

        for label, interval in timeframe_settings:
            try:
                result = detect_candlestick_patterns(interval)

                pattern_rows.append({
                    "Timeframe": label,
                    "Last closed price": result["close"],
                    "Detected patterns": ", ".join(result["patterns"]),
                })

            except Exception as exc:
                pattern_rows.append({
                    "Timeframe": label,
                    "Last closed price": None,
                    "Detected patterns": f"Unavailable: {exc}",
                })

        pattern_df = pd.DataFrame(pattern_rows)

        st.dataframe(
            pattern_df,
            use_container_width=True,
            hide_index=True,
        )

        st.caption(
            "Patterns are rule-based observations on completed candles. "
            "They are not standalone entry signals."
        )

        # ----------------------------------------------
        # Liquidity heatmap: sample and retain snapshots
        # ----------------------------------------------
        bids, asks, mid_price = get_orderbook()

        zones = detect_liquidity_zones(bids, asks)

        st.subheader("Unusually large visible liquidity levels")

        if zones.empty:
            st.info("No qualifying liquidity zones detected.")
        else:
            st.dataframe(
                zones,
                use_container_width=True,
                hide_index=True,
            )

        # Aggregate onto a fixed 50-tick grid. A grid based on the live
        # mid price shifts every refresh and splits one price across rows.
        price_filter = instrument.get("priceFilter") or {}
        tick_text = str(price_filter.get("tickSize") or "0.01")
        price_band = float(liquidity_band_width(tick_text))

        now = datetime.now(timezone.utc)

        # Positive = bids; negative = asks.
        bid_bins = aggregate_liquidity(bids, tick_text)
        ask_bins = aggregate_liquidity(asks, tick_text)

        snapshot = bid_bins.to_dict()

        for price_level, quantity in ask_bins.items():
            snapshot[price_level] = (
                snapshot.get(price_level, 0.0) - quantity
            )

        # Persist every sampled order-book snapshot.
        save_snapshot(
            SYMBOL,
            now,
            mid_price,
            snapshot,
        )

        # Load recent history from SQLite, not session memory.
        liquidity_history = load_recent_snapshots(
            SYMBOL,
            limit=60,
        )

        persistent_zones = track_persistent_zones(
            liquidity_history,
            min_observations=3,
        )

        st.subheader("Repeatedly observed liquidity zones")

        if persistent_zones.empty:
            st.info(
                "Not enough repeated large-liquidity observations yet. "
                "Leave the monitor running to collect more snapshots."
            )
        else:
            st.dataframe(
                persistent_zones,
                use_container_width=True,
                hide_index=True,
            )

        st.subheader("Confluence engine")

        assessment = assess_confluence(
            trend_df,
            pattern_df,
            persistent_zones,
        )

        st.write(f"**Assessment:** {assessment['status']}")

        col1, col2 = st.columns(2)

        with col1:
            st.metric(
                "Bullish evidence points",
                assessment["bullish_points"],
            )

        with col2:
            st.metric(
                "Bearish evidence points",
                assessment["bearish_points"],
            )

        st.write("**Reasoning**")

        for reason in assessment["reasons"]:
            st.write(f"- {reason}")

        st.caption(
            "Advisory analysis only. Scores are heuristic, not probabilities. "
            "No automated orders are enabled."
        )

        st.divider()
        st.subheader("AI-style market briefing")

        briefing = generate_market_briefing(
            trend_df,
            pattern_df,
            assessment,
            persistent_zones,
        )

        st.markdown(briefing)

        st.caption(
            "Rule-based explanation, not a live AI prediction. "
            "Scores are heuristic, not probabilities. "
            "Alerts only — no automated orders."
        )

        st.subheader("Live order-book liquidity heatmap")
        st.caption(
            "Each column is a sampled order-book snapshot. "
            "Blue is displayed bid size and red is displayed ask size. "
            "Prices are grouped in fixed 50-tick bands. "
            "This is visible resting liquidity, not stop-loss data."
        )

        history_rows = []

        for sample in liquidity_history:
            sample_time = pd.to_datetime(sample["time"], utc=True)

            for level, quantity in sample["levels"].items():
                history_rows.append({
                    "time": sample_time,
                    "price_band": float(level),
                    "signed_quantity": float(quantity),
                })

        if history_rows:
            history_df = pd.DataFrame(history_rows)

            heatmap_data = history_df.pivot_table(
                index="price_band",
                columns="time",
                values="signed_quantity",
                aggfunc="sum",
                fill_value=0,
            ).sort_index()

            # Focus the display around the current market price.
            nearby = heatmap_data.loc[
                (heatmap_data.index >= mid_price - 20 * price_band)
                & (heatmap_data.index <= mid_price + 20 * price_band)
            ]

            if not nearby.empty:
                heatmap = go.Figure(
                    data=go.Heatmap(
                        x=nearby.columns,
                        y=nearby.index,
                        z=nearby.values,
                        colorscale="RdBu",
                        zmid=0,
                        colorbar={"title": "Signed quantity"},
                        hovertemplate=(
                            "Time: %{x}<br>"
                            "Price band: %{y:.4f}<br>"
                            "Signed quantity: %{z:.4f}"
                            "<extra></extra>"
                        ),
                    )
                )

                heatmap.update_layout(
                    title="Resting liquidity by price and time",
                    xaxis_title="Snapshot time (UTC)",
                    yaxis_title="Price (USDT)",
                    height=550,
                )

                st.plotly_chart(
                    heatmap,
                    width="stretch",
                )
            else:
                st.info("No heatmap levels in the selected price range.")

        c1, c2 = st.columns(2)

        with c1:
            st.write("**Largest visible bid levels**")
            st.dataframe(
                bids.nlargest(10, "size")[["price", "size"]].reset_index(drop=True),
                width="stretch",
            )

        with c2:
            st.write("**Largest visible ask levels**")
            st.dataframe(
                asks.nlargest(10, "size")[["price", "size"]].reset_index(drop=True),
                width="stretch",
            )

        bid_total = bids["size"].sum()
        ask_total = asks["size"].sum()
        total = bid_total + ask_total

        if total > 0:
            imbalance = (bid_total - ask_total) / total

            st.metric(
                "Visible order-book imbalance",
                f"{imbalance:+.1%}",
            )

            st.caption(
                "Positive means more displayed bid quantity than ask "
                "quantity within the retrieved levels. It is not, by "
                "itself, a buy signal."
            )

        # Basic rule-based alert
        status, reason = classify_setup(candles)

        st.subheader("Market alert")
        st.write(f"**{status}**")
        st.write(reason)

        st.info(
            "This is a rule-based educational alert, not an AI prediction "
            "or a recommendation to enter a trade. No orders are placed."
        )

        with st.expander("Latest candle data"):
            st.dataframe(
                candles.tail(20).sort_values("time", ascending=False),
                width="stretch",
            )

        with st.expander("Verified contract specifications"):
            st.json({
                "symbol": instrument.get("symbol"),
                "contractType": instrument.get("contractType"),
                "status": instrument.get("status"),
                "baseCoin": instrument.get("baseCoin"),
                "quoteCoin": instrument.get("quoteCoin"),
                "settleCoin": instrument.get("settleCoin"),
                "tickSize": instrument.get("priceFilter", {}).get("tickSize"),
                "qtyStep": instrument.get("lotSizeFilter", {}).get("qtyStep"),
            })

        st.caption(
            "Data source: Bybit public API. All displayed times are UTC. "
            "The page refreshes about every 5 seconds."
        )

    except (requests.RequestException, RuntimeError, KeyError, ValueError, TypeError, AttributeError) as exc:
        st.error(f"Market connection or validation failed: {exc}")
        st.caption(
            "Check your internet connection, symbol availability and "
            "Bybit API response. The app will not substitute another symbol."
        )

render_market()
