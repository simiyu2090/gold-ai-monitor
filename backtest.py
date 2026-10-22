import time
from datetime import datetime, timedelta, timezone

import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st


API_URL = "https://api.bybit.com/v5/market/kline"
SYMBOL = "XAUTUSDT"
CATEGORY = "linear"
INTERVAL = "5"


@st.cache_data(ttl=300, show_spinner=False)
def fetch_history(days):
    end_time = datetime.now(timezone.utc)
    start_time = end_time - timedelta(days=days)

    start_ms = int(start_time.timestamp() * 1000)
    cursor = int(end_time.timestamp() * 1000)

    all_rows = []
    session = requests.Session()

    while cursor > start_ms:
        response = session.get(
            API_URL,
            params={
                "category": CATEGORY,
                "symbol": SYMBOL,
                "interval": INTERVAL,
                "start": start_ms,
                "end": cursor,
                "limit": 1000,
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()

        if payload.get("retCode") != 0:
            raise RuntimeError(
                payload.get("retMsg", "Bybit API error")
            )

        rows = payload.get("result", {}).get("list", [])

        if not rows:
            break

        all_rows.extend(rows)

        oldest_ms = min(int(row[0]) for row in rows)

        if oldest_ms <= start_ms:
            break

        if oldest_ms >= cursor:
            break

        cursor = oldest_ms - 1

        # Avoid sending requests too rapidly.
        time.sleep(0.1)

    session.close()

    if not all_rows:
        raise ValueError(
            "Bybit returned no historical candles."
        )

    df = pd.DataFrame(
        all_rows,
        columns=[
            "timestamp",
            "open",
            "high",
            "low",
            "close",
            "volume",
            "turnover",
        ],
    )

    df["timestamp"] = pd.to_numeric(
        df["timestamp"], errors="coerce"
    )

    for column in [
        "open", "high", "low", "close", "volume"
    ]:
        df[column] = pd.to_numeric(
            df[column], errors="coerce"
        )

    df = df.dropna(
        subset=["timestamp", "open", "high", "low", "close"]
    )

    df = df.drop_duplicates(
        subset=["timestamp"]
    ).sort_values("timestamp")

    df = df[
        (df["timestamp"] >= start_ms)
        & (df["timestamp"] < int(end_time.timestamp() * 1000))
    ].copy()

    df["timestamp"] = pd.to_datetime(
        df["timestamp"], unit="ms", utc=True
    )

    # Exclude the newest candle if it is still forming.
    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    latest_open_ms = int(
        df["timestamp"].iloc[-1].timestamp() * 1000
    )

    if now_ms < latest_open_ms + 5 * 60 * 1000:
        df = df.iloc[:-1].copy()

    df = df.reset_index(drop=True)

    if len(df) < 60:
        raise ValueError(
            "Not enough completed candles for the test. "
            "Try a longer date range or check symbol availability."
        )

    return df


def run_backtest(
    candles,
    fee_rate,
    slippage_rate,
):
    df = candles.copy()

    df["ema20"] = df["close"].ewm(
        span=20, adjust=False
    ).mean()

    df["ema50"] = df["close"].ewm(
        span=50, adjust=False
    ).mean()

    # Simple illustrative rules:
    # Long when close > EMA20 > EMA50.
    # Short when close < EMA20 < EMA50.
    df["signal"] = 0

    long_condition = (
        (df["close"] > df["ema20"])
        & (df["ema20"] > df["ema50"])
    )

    short_condition = (
        (df["close"] < df["ema20"])
        & (df["ema20"] < df["ema50"])
    )

    df.loc[long_condition, "signal"] = 1
    df.loc[short_condition, "signal"] = -1

    # Use the previous candle's signal to avoid
    # trading on information before its close.
    df["position"] = df["signal"].shift(1).fillna(0)

    df["market_return"] = df["close"].pct_change().fillna(0)

    # A change from long to short counts as two sides:
    # closing the long and opening the short.
    df["turnover"] = df["position"].diff().abs()
    df.loc[df.index[0], "turnover"] = abs(
        df.loc[df.index[0], "position"]
    )
    df["turnover"] = df["turnover"].fillna(0)

    # Rates are per side, applied to traded notional.
    cost_rate = fee_rate + slippage_rate

    df["gross_return"] = (
        df["position"] * df["market_return"]
    )

    df["cost"] = df["turnover"] * cost_rate

    df["net_return"] = df["gross_return"] - df["cost"]

    df["strategy_equity"] = (
        1 + df["net_return"]
    ).cumprod()

    df["buy_hold_equity"] = (
        1 + df["market_return"]
    ).cumprod()

    df["running_peak"] = df["strategy_equity"].cummax()

    df["drawdown"] = (
        df["strategy_equity"] / df["running_peak"]
    ) - 1

    return df


st.set_page_config(
    page_title="Gold Strategy Backtester",
    layout="wide",
)

st.title("Gold Strategy Backtester")
st.caption(
    "Bybit XAUTUSDT perpetual | 5-minute candles | "
    "Historical simulation only"
)

st.warning(
    "Educational test, not a trading recommendation. "
    "Fees, slippage, funding, liquidity constraints, and "
    "live execution can materially change results."
)

st.sidebar.header("Backtest settings")

days = st.sidebar.selectbox(
    "Historical period",
    options=[7, 14, 30, 60],
    index=2,
)

fee_bps = st.sidebar.number_input(
    "Trading fee per side (basis points)",
    min_value=0.0,
    max_value=100.0,
    value=5.5,
    step=0.5,
    help="5.5 basis points equals 0.055% per side.",
)

slippage_bps = st.sidebar.number_input(
    "Estimated slippage per side (basis points)",
    min_value=0.0,
    max_value=100.0,
    value=2.0,
    step=0.5,
)

st.sidebar.caption(
    "Verify your actual Bybit fee tier. These default "
    "costs are illustrative, not a quote."
)

if st.button("Run backtest", type="primary"):
    try:
        with st.spinner("Downloading historical candles..."):
            candles = fetch_history(days)

        results = run_backtest(
            candles,
            fee_rate=fee_bps / 10000,
            slippage_rate=slippage_bps / 10000,
        )

        # Module 9: Holdout validation
        # Reserve the final 30% of candles for evaluation.

        split_index = int(len(candles) * 0.70)

        if split_index < 60 or len(candles) - split_index < 30:
            raise ValueError(
                "Not enough candles for holdout validation. "
                "Choose a longer historical period."
            )

        # Include 60 earlier candles to warm up the EMAs.
        warmup_start = max(0, split_index - 60)

        holdout_full = run_backtest(
            candles.iloc[warmup_start:].copy(),
            fee_rate=fee_bps / 10000,
            slippage_rate=slippage_bps / 10000,
        )

        # Evaluate only candles in the final 30%.
        holdout = holdout_full.iloc[
            split_index - warmup_start:
        ].copy()

        if holdout.empty:
            raise ValueError("The holdout period is empty.")

        holdout["test_equity"] = (
            1 + holdout["net_return"]
        ).cumprod()

        holdout["test_buy_hold"] = (
            1 + holdout["market_return"]
        ).cumprod()

        holdout["test_peak"] = (
            holdout["test_equity"].cummax()
        )

        holdout["test_drawdown"] = (
            holdout["test_equity"] / holdout["test_peak"]
        ) - 1

        holdout_return = (
            holdout["test_equity"].iloc[-1] - 1
        ) * 100

        holdout_buy_hold = (
            holdout["test_buy_hold"].iloc[-1] - 1
        ) * 100

        holdout_max_drawdown = (
            holdout["test_drawdown"].min()
        ) * 100

        strategy_return = (
            results["strategy_equity"].iloc[-1] - 1
        ) * 100

        buy_hold_return = (
            results["buy_hold_equity"].iloc[-1] - 1
        ) * 100

        max_drawdown = (
            results["drawdown"].min() * 100
        )

        position_changes = (
            results["position"].diff().abs().fillna(
                abs(results["position"].iloc[0])
            )
        )

        trade_sides = int(position_changes.sum())

        col1, col2, col3, col4 = st.columns(4)

        col1.metric(
            "Strategy net return",
            f"{strategy_return:.2f}%",
        )

        col2.metric(
            "Buy-and-hold return",
            f"{buy_hold_return:.2f}%",
        )

        col3.metric(
            "Maximum drawdown",
            f"{max_drawdown:.2f}%",
        )

        col4.metric(
            "Position-change sides",
            f"{trade_sides}",
        )

        st.caption(
            f"Completed candles: {len(results):,} | "
            f"Period: {days} days | "
            f"Estimated costs: {fee_bps + slippage_bps:.1f} "
            "basis points per traded side."
        )

        fig = go.Figure()

        fig.add_trace(
            go.Scatter(
                x=results["timestamp"],
                y=results["strategy_equity"],
                name="EMA strategy (net of estimated costs)",
            )
        )

        fig.add_trace(
            go.Scatter(
                x=results["timestamp"],
                y=results["buy_hold_equity"],
                name="Buy and hold",
            )
        )

        fig.update_layout(
            title="Growth of 1 unit of starting capital",
            xaxis_title="Time (UTC)",
            yaxis_title="Equity multiple",
            hovermode="x unified",
        )

        st.plotly_chart(fig, use_container_width=True)

        st.divider()
        st.subheader("Module 9 — Holdout validation")
        st.caption(
            "Results from the final 30% of historical candles, "
            "after estimated fees and slippage."
        )

        col1, col2, col3 = st.columns(3)

        col1.metric(
            "Holdout strategy return",
            f"{holdout_return:.2f}%",
        )

        col2.metric(
            "Holdout buy-and-hold return",
            f"{holdout_buy_hold:.2f}%",
        )

        col3.metric(
            "Holdout maximum drawdown",
            f"{holdout_max_drawdown:.2f}%",
        )

        holdout_fig = go.Figure()

        holdout_fig.add_trace(
            go.Scatter(
                x=holdout["timestamp"],
                y=holdout["test_equity"],
                name="EMA strategy",
            )
        )

        holdout_fig.add_trace(
            go.Scatter(
                x=holdout["timestamp"],
                y=holdout["test_buy_hold"],
                name="Buy and hold",
            )
        )

        holdout_fig.update_layout(
            title="Holdout period: growth of 1 unit",
            xaxis_title="Time (UTC)",
            yaxis_title="Equity multiple",
            hovermode="x unified",
        )

        st.plotly_chart(
            holdout_fig,
            use_container_width=True,
        )

        st.info(
            "Interpretation: the holdout period is a separate "
            "evaluation window, not a guarantee of future performance. "
            "The EMA rules were not optimized on the first 70%; "
            "the holdout helps assess how the fixed rules behaved "
            "in a later period."
        )

        st.subheader("Recent simulated results")

        st.dataframe(
            results[
                [
                    "timestamp",
                    "close",
                    "ema20",
                    "ema50",
                    "position",
                    "net_return",
                    "strategy_equity",
                    "drawdown",
                ]
            ].tail(20),
            use_container_width=True,
        )

        st.info(
            "This is a baseline EMA test, not yet a backtest "
            "of the liquidity or confluence signals from your "
            "main dashboard. It does not yet model funding, "
            "spread, stop-loss execution, or liquidation."
        )

    except Exception as exc:
        st.error(f"Backtest failed: {exc}")
        st.caption(
            "Check your internet connection, the XAUTUSDT "
            "contract, and the terminal error if one appears."
        )
