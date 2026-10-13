"""Price endpoints with the data sources stubbed, so no network and no real database."""
from datetime import date

import pytest

import sources
import storage


@pytest.fixture
def fetches(monkeypatch):
    """Records every (source, symbol, start, end) the app asks a source for."""
    calls = []
    failing = set()

    def fake(source, symbol, start, end):
        calls.append((source, symbol, start, end))
        if symbol in failing:
            raise ValueError(f"Yahoo Finance returned no data for {symbol}. Check the ticker.")
        days = (end - start).days
        return [
            {"date": date.fromordinal(start.toordinal() + i).isoformat(), "open": 1.0, "high": 2.0,
             "low": 0.5, "close": 1.5, "adj_close": 1.5, "volume": 10.0}
            for i in range(days)
        ]

    monkeypatch.setattr(sources, "fetch", fake)
    fake.calls, fake.failing = calls, failing
    return fake


def post_stocks(client, **body):
    return client.post("/api/stocks/fetch", json=body)


def test_fetch_stores_prices_and_reports_counts(client, fetches):
    resp = post_stocks(client, symbols=["aapl"], start="2026-01-01", end="2026-01-10")
    result = resp.get_json()["results"][0]
    assert result["ok"] and result["symbol"] == "AAPL"
    # The chosen end day is included, so Jan 1 to Jan 10 is ten days.
    assert result["rows"] == result["fetched"] == 10 and result["cached"] == 0
    assert fetches.calls == [("yahoo", "AAPL", date(2026, 1, 1), date(2026, 1, 11))]
    assert len(client.get("/api/prices?symbol=AAPL").get_json()["rows"]) == 10


def test_second_fetch_of_same_range_uses_the_cache(client, fetches):
    post_stocks(client, symbols=["AAPL"], start="2026-01-01", end="2026-01-10")
    result = post_stocks(client, symbols=["AAPL"], start="2026-01-01", end="2026-01-10").get_json()["results"][0]
    assert len(fetches.calls) == 1
    assert result["fetched"] == 0 and result["cached"] == 10


def test_extending_the_range_fetches_only_the_new_part(client, fetches):
    post_stocks(client, symbols=["AAPL"], start="2026-01-01", end="2026-01-10")
    result = post_stocks(client, symbols=["AAPL"], start="2026-01-01", end="2026-01-15").get_json()["results"][0]
    assert fetches.calls[1] == ("yahoo", "AAPL", date(2026, 1, 11), date(2026, 1, 16))
    assert result["fetched"] == 5 and result["cached"] == 10 and result["rows"] == 15


def test_one_bad_symbol_does_not_fail_the_batch(client, fetches):
    fetches.failing.add("ZZZZ")
    results = post_stocks(client, symbols=["AAPL", "ZZZZ"], start="2026-01-01", end="2026-01-05").get_json()["results"]
    assert [r["ok"] for r in results] == [True, False]
    assert "no data for ZZZZ" in results[1]["error"]
    # A failed fetch must not be recorded as covered, or it could never be retried.
    assert storage.missing_ranges("ZZZZ", "yahoo", date(2026, 1, 1), date(2026, 1, 6)) != []


def test_fetch_defaults_to_the_watchlist(client, fetches):
    client.post("/api/watchlist", json={"symbols": "msft, nvda"})
    results = post_stocks(client, start="2026-01-01", end="2026-01-05").get_json()["results"]
    assert [r["symbol"] for r in results] == ["MSFT", "NVDA"]


def test_fetch_with_empty_watchlist_is_a_400(client, fetches):
    resp = post_stocks(client, start="2026-01-01", end="2026-01-05")
    assert resp.status_code == 400 and "watchlist is empty" in resp.get_json()["error"]


def test_start_must_be_before_end(client, fetches):
    resp = post_stocks(client, symbols=["AAPL"], start="2026-02-01", end="2026-01-01")
    assert resp.status_code == 400 and "before end" in resp.get_json()["error"]
    assert fetches.calls == []


def test_malformed_date_is_a_400(client, fetches):
    resp = post_stocks(client, symbols=["AAPL"], start="01/02/2026", end="2026-01-05")
    assert resp.status_code == 400


def test_bad_ticker_is_a_400(client, fetches):
    resp = post_stocks(client, symbols=["not a ticker!"])
    assert resp.status_code == 400 and "ticker" in resp.get_json()["error"]


def test_default_range_is_the_last_year(client, fetches):
    post_stocks(client, symbols=["AAPL"])
    _, _, start, end = fetches.calls[0]
    assert end == date.fromordinal(date.today().toordinal() + 1)
    assert (end - start).days == 366


# ---------- indicators ----------

def test_indicator_list_includes_keys(client):
    keys = {i["key"] for i in client.get("/api/indicators").get_json()}
    assert {"sp500", "wti_spot", "vix"} <= keys


def test_indicator_fetch_uses_its_own_source_and_symbol(client, fetches):
    resp = client.post("/api/indicators/fetch", json={
        "keys": ["sp500", "wti_spot"], "start": "2026-01-01", "end": "2026-01-05"})
    results = resp.get_json()["results"]
    assert [(r["key"], r["symbol"], r["ok"]) for r in results] == [
        ("sp500", "^GSPC", True), ("wti_spot", "DCOILWTICO", True)]
    assert [(c[0], c[1]) for c in fetches.calls] == [("yahoo", "^GSPC"), ("fred", "DCOILWTICO")]


def test_indicator_fetch_validates_input(client, fetches):
    assert client.post("/api/indicators/fetch", json={"keys": []}).status_code == 400
    resp = client.post("/api/indicators/fetch", json={"keys": ["sp500", "bogus"]})
    assert resp.status_code == 400 and "bogus" in resp.get_json()["error"]
    assert fetches.calls == []


# ---------- reading ----------

def test_prices_requires_a_symbol(client):
    assert client.get("/api/prices").status_code == 400


def test_prices_honours_start_and_end(client, fetches):
    post_stocks(client, symbols=["AAPL"], start="2026-01-01", end="2026-01-10")
    rows = client.get("/api/prices?symbol=AAPL&start=2026-01-03&end=2026-01-05").get_json()["rows"]
    assert [r["date"] for r in rows] == ["2026-01-03", "2026-01-04", "2026-01-05"]


def test_stored_summary_endpoint(client, fetches):
    assert client.get("/api/stored").get_json() == []
    post_stocks(client, symbols=["AAPL"], start="2026-01-01", end="2026-01-10")
    summary = client.get("/api/stored").get_json()
    assert [(s["symbol"], s["rows"], s["first_date"], s["last_date"]) for s in summary] == [
        ("AAPL", 10, "2026-01-01", "2026-01-10")]


# ---------- watchlist ----------

def test_watchlist_accepts_messy_input_and_dedupes(client):
    resp = client.post("/api/watchlist", json={"symbols": "aapl, MSFT;nvda  aapl"})
    assert resp.get_json() == ["AAPL", "MSFT", "NVDA"]


def test_watchlist_accepts_a_list(client):
    assert client.post("/api/watchlist", json={"symbols": ["aapl"]}).get_json() == ["AAPL"]


def test_watchlist_rejects_empty_and_invalid(client):
    assert client.post("/api/watchlist", json={"symbols": "  "}).status_code == 400
    assert client.post("/api/watchlist", json={"symbols": "bad!ticker"}).status_code == 400


def test_watchlist_delete(client):
    client.post("/api/watchlist", json={"symbols": "AAPL, MSFT"})
    assert client.delete("/api/watchlist/aapl").get_json() == ["MSFT"]
