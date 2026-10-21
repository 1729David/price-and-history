"""Fetchers with Yahoo Finance and FRED stubbed out: no network."""
from datetime import date

import pandas as pd
import pytest

import sources

START, END = date(2026, 1, 1), date(2026, 2, 1)


def yahoo_frame():
    index = pd.DatetimeIndex(["2026-01-02", "2026-01-05"], tz="America/New_York")
    return pd.DataFrame({
        "Open": [10.0, 11.0], "High": [12.0, 13.0], "Low": [9.0, 10.0],
        "Close": [11.0, 12.0], "Adj Close": [10.5, float("nan")], "Volume": [1000, 2000],
    }, index=index)


def stub_yahoo(monkeypatch, frame):
    calls = []

    class FakeTicker:
        def __init__(self, symbol):
            calls.append(("ticker", symbol))

        def history(self, **kwargs):
            calls.append(("history", kwargs))
            return frame

    monkeypatch.setattr(sources.yf, "Ticker", FakeTicker)
    return calls


def test_yahoo_rows_have_the_common_shape(monkeypatch):
    calls = stub_yahoo(monkeypatch, yahoo_frame())
    rows = sources.fetch_yahoo("AAPL", START, END)
    assert rows[0] == {"date": "2026-01-02", "open": 10.0, "high": 12.0, "low": 9.0,
                       "close": 11.0, "adj_close": 10.5, "volume": 1000.0}
    assert ("ticker", "AAPL") in calls
    # Both Close and Adj Close are needed, so Yahoo's auto-adjust must stay off.
    assert calls[1][1] == {"start": START, "end": END, "auto_adjust": False}


def test_yahoo_nan_becomes_none(monkeypatch):
    stub_yahoo(monkeypatch, yahoo_frame())
    assert sources.fetch_yahoo("AAPL", START, END)[1]["adj_close"] is None


def test_yahoo_missing_adj_close_column_falls_back_to_close(monkeypatch):
    stub_yahoo(monkeypatch, yahoo_frame().drop(columns=["Adj Close"]))
    assert [r["adj_close"] for r in sources.fetch_yahoo("^GSPC", START, END)] == [11.0, 12.0]


def test_yahoo_empty_result_is_a_clear_error(monkeypatch):
    stub_yahoo(monkeypatch, pd.DataFrame())
    with pytest.raises(ValueError, match="no data for ZZZZ"):
        sources.fetch_yahoo("ZZZZ", START, END)


class FakeResponse:
    def __init__(self, text, ok=True):
        self.text, self._ok = text, ok

    def raise_for_status(self):
        if not self._ok:
            raise RuntimeError("503")


def stub_fred(monkeypatch, text, ok=True):
    seen = {}

    def get(url, params, timeout):
        seen.update(url=url, params=params)
        return FakeResponse(text, ok)

    monkeypatch.setattr(sources.requests, "get", get)
    return seen


def test_fred_uses_close_only_and_drops_missing_days(monkeypatch):
    seen = stub_fred(monkeypatch, "observation_date,DCOILWTICO\n2026-01-02,70.5\n2026-01-03,.\n2026-01-06,71\n")
    rows = sources.fetch_fred("DCOILWTICO", START, END)
    assert rows == [
        {"date": "2026-01-02", "open": None, "high": None, "low": None,
         "close": 70.5, "adj_close": 70.5, "volume": None},
        {"date": "2026-01-06", "open": None, "high": None, "low": None,
         "close": 71.0, "adj_close": 71.0, "volume": None},
    ]
    assert seen["params"] == {"id": "DCOILWTICO", "cosd": "2026-01-01", "coed": "2026-02-01"}


def test_fred_with_only_missing_days_returns_no_rows(monkeypatch):
    # Not an error: a range of holidays legitimately has no values. Only a completely
    # empty response (below) is treated as a bad series id.
    stub_fred(monkeypatch, "observation_date,DCOILWTICO\n2026-01-03,.\n")
    assert sources.fetch_fred("DCOILWTICO", START, END) == []


def test_fred_empty_response_is_a_clear_error(monkeypatch):
    stub_fred(monkeypatch, "observation_date,DCOILWTICO\n")
    with pytest.raises(ValueError, match="no data for DCOILWTICO"):
        sources.fetch_fred("DCOILWTICO", START, END)


def test_fred_http_error_propagates(monkeypatch):
    stub_fred(monkeypatch, "", ok=False)
    with pytest.raises(RuntimeError):
        sources.fetch_fred("DCOILWTICO", START, END)


def test_fetch_dispatches_by_source(monkeypatch):
    monkeypatch.setattr(sources, "fetch_yahoo", lambda *a: "yahoo")
    monkeypatch.setattr(sources, "fetch_fred", lambda *a: "fred")
    assert sources.fetch("yahoo", "AAPL", START, END) == "yahoo"
    assert sources.fetch("fred", "DCOILWTICO", START, END) == "fred"
    with pytest.raises(ValueError, match="Unknown source"):
        sources.fetch("bloomberg", "AAPL", START, END)


def test_every_indicator_names_a_known_source():
    for key, meta in sources.INDICATORS.items():
        assert meta["source"] in {"yahoo", "fred"}, key
        assert meta["symbol"] and meta["name"] and meta["unit"], key
