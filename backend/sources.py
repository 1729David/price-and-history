"""Data sources: Yahoo Finance (via yfinance) and FRED (via its public CSV endpoint).

Every fetcher returns a list of dicts with the same shape so storage doesn't
care where the data came from:
    {date, open, high, low, close, adj_close, volume}
FRED series only have a single value per day, so it is stored in `close`.
"""
from __future__ import annotations

import io
from datetime import date

import pandas as pd
import requests
import yfinance as yf

# Market indicators offered in the UI dropdown.
# key -> metadata. `symbol` is the Yahoo ticker or the FRED series id.
INDICATORS: dict[str, dict] = {
    "sp500": {"name": "S&P 500 index", "source": "yahoo", "symbol": "^GSPC", "unit": "points"},
    "gold_futures": {"name": "Gold futures", "source": "yahoo", "symbol": "GC=F", "unit": "USD/oz"},
    "wti_futures": {"name": "WTI crude oil futures", "source": "yahoo", "symbol": "CL=F", "unit": "USD/bbl"},
    "brent_futures": {"name": "Brent crude oil futures", "source": "yahoo", "symbol": "BZ=F", "unit": "USD/bbl"},
    "wti_spot": {"name": "WTI crude oil spot (FRED)", "source": "fred", "symbol": "DCOILWTICO", "unit": "USD/bbl"},
    "brent_spot": {"name": "Brent crude oil spot (FRED)", "source": "fred", "symbol": "DCOILBRENTEU", "unit": "USD/bbl"},
    "gold_etf": {"name": "Gold ETF (GLD)", "source": "yahoo", "symbol": "GLD", "unit": "USD"},
    "vix": {"name": "Volatility index (VIX)", "source": "yahoo", "symbol": "^VIX", "unit": "points"},
}

FRED_CSV_URL = "https://fred.stlouisfed.org/graph/fredgraph.csv"


def _num(value):
    """Convert pandas/numpy values to plain floats, mapping NaN to None."""
    if value is None or pd.isna(value):
        return None
    return float(value)


def fetch_yahoo(symbol: str, start: date, end: date) -> list[dict]:
    # auto_adjust=False keeps both Close and Adj Close columns.
    df = yf.Ticker(symbol).history(start=start, end=end, auto_adjust=False)
    if df.empty:
        raise ValueError(f"Yahoo Finance returned no data for {symbol}. Check the ticker.")

    rows = []
    for ts, r in df.iterrows():
        rows.append({
            "date": ts.date().isoformat(),
            "open": _num(r.get("Open")),
            "high": _num(r.get("High")),
            "low": _num(r.get("Low")),
            "close": _num(r.get("Close")),
            "adj_close": _num(r.get("Adj Close", r.get("Close"))),
            "volume": _num(r.get("Volume")),
        })
    return rows


def fetch_fred(series_id: str, start: date, end: date) -> list[dict]:
    resp = requests.get(
        FRED_CSV_URL,
        params={"id": series_id, "cosd": start.isoformat(), "coed": end.isoformat()},
        timeout=30,
    )
    resp.raise_for_status()
    df = pd.read_csv(io.StringIO(resp.text))
    if df.empty:
        raise ValueError(f"FRED returned no data for {series_id}.")

    date_col, value_col = df.columns[0], df.columns[1]
    # FRED marks missing days with "." which becomes NaN here.
    df[value_col] = pd.to_numeric(df[value_col], errors="coerce")
    df = df.dropna(subset=[value_col])

    return [
        {"date": str(d)[:10], "open": None, "high": None, "low": None,
         "close": float(v), "adj_close": float(v), "volume": None}
        for d, v in zip(df[date_col], df[value_col])
    ]


def fetch(source: str, symbol: str, start: date, end: date) -> list[dict]:
    if source == "yahoo":
        return fetch_yahoo(symbol, start, end)
    if source == "fred":
        return fetch_fred(symbol, start, end)
    raise ValueError(f"Unknown source: {source}")
