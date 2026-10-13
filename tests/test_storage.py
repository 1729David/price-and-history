from datetime import date

import storage

D = date.fromisoformat


def row(day, close=1.0, **extra):
    return {"date": day, "open": close, "high": close, "low": close, "close": close,
            "adj_close": close, "volume": 100.0, **extra}


# ---------- missing_ranges ----------

def test_nothing_covered_means_whole_range_missing(db):
    assert storage.missing_ranges("AAPL", "yahoo", D("2026-01-01"), D("2026-02-01")) == [
        (D("2026-01-01"), D("2026-02-01"))
    ]


def test_fully_covered_range_has_nothing_missing(db):
    storage.add_coverage("AAPL", "yahoo", D("2026-01-01"), D("2026-03-01"))
    assert storage.missing_ranges("AAPL", "yahoo", D("2026-01-10"), D("2026-02-01")) == []


def test_gaps_before_between_and_after_coverage(db):
    storage.add_coverage("AAPL", "yahoo", D("2026-01-10"), D("2026-01-20"))
    storage.add_coverage("AAPL", "yahoo", D("2026-02-01"), D("2026-02-10"))
    assert storage.missing_ranges("AAPL", "yahoo", D("2026-01-01"), D("2026-02-20")) == [
        (D("2026-01-01"), D("2026-01-10")),
        (D("2026-01-20"), D("2026-02-01")),
        (D("2026-02-10"), D("2026-02-20")),
    ]


def test_coverage_is_per_symbol_and_source(db):
    storage.add_coverage("AAPL", "yahoo", D("2026-01-01"), D("2026-02-01"))
    whole = [(D("2026-01-01"), D("2026-02-01"))]
    assert storage.missing_ranges("MSFT", "yahoo", D("2026-01-01"), D("2026-02-01")) == whole
    assert storage.missing_ranges("AAPL", "fred", D("2026-01-01"), D("2026-02-01")) == whole


# ---------- add_coverage ----------

def coverage():
    with storage.connect() as conn:
        return [(r["start_date"], r["end_date"]) for r in conn.execute(
            "SELECT start_date, end_date FROM fetch_coverage ORDER BY start_date")]


def test_overlapping_and_adjacent_coverage_merges(db):
    storage.add_coverage("AAPL", "yahoo", D("2026-01-01"), D("2026-01-10"))
    storage.add_coverage("AAPL", "yahoo", D("2026-01-05"), D("2026-01-20"))  # overlaps
    storage.add_coverage("AAPL", "yahoo", D("2026-01-20"), D("2026-01-25"))  # adjacent
    assert coverage() == [("2026-01-01", "2026-01-25")]


def test_disjoint_coverage_stays_separate(db):
    storage.add_coverage("AAPL", "yahoo", D("2026-01-01"), D("2026-01-10"))
    storage.add_coverage("AAPL", "yahoo", D("2026-02-01"), D("2026-02-10"))
    assert len(coverage()) == 2


def test_covering_a_subrange_changes_nothing(db):
    storage.add_coverage("AAPL", "yahoo", D("2026-01-01"), D("2026-03-01"))
    storage.add_coverage("AAPL", "yahoo", D("2026-01-10"), D("2026-01-20"))
    assert coverage() == [("2026-01-01", "2026-03-01")]


# ---------- upsert_prices / get_prices ----------

def test_upsert_then_read_back_in_date_order(db):
    storage.upsert_prices("AAPL", "yahoo", [row("2026-01-03", 3), row("2026-01-02", 2)])
    got = storage.get_prices("AAPL")
    assert [r["date"] for r in got] == ["2026-01-02", "2026-01-03"]
    assert got[0]["close"] == 2 and got[0]["volume"] == 100.0


def test_refetching_a_day_updates_instead_of_duplicating(db):
    storage.upsert_prices("AAPL", "yahoo", [row("2026-01-02", 2)])
    storage.upsert_prices("AAPL", "yahoo", [row("2026-01-02", 5)])
    got = storage.get_prices("AAPL")
    assert len(got) == 1 and got[0]["close"] == 5


def test_get_prices_filters_by_inclusive_date_bounds(db):
    storage.upsert_prices("AAPL", "yahoo", [row(f"2026-01-0{d}") for d in range(1, 6)])
    got = storage.get_prices("AAPL", "2026-01-02", "2026-01-04")
    assert [r["date"] for r in got] == ["2026-01-02", "2026-01-03", "2026-01-04"]
    assert [r["date"] for r in storage.get_prices("AAPL", start="2026-01-05")] == ["2026-01-05"]
    assert [r["date"] for r in storage.get_prices("AAPL", end="2026-01-01")] == ["2026-01-01"]


def test_symbols_are_kept_apart(db):
    storage.upsert_prices("AAPL", "yahoo", [row("2026-01-02", 1)])
    storage.upsert_prices("MSFT", "yahoo", [row("2026-01-02", 9)])
    assert storage.get_prices("AAPL")[0]["close"] == 1
    assert storage.get_prices("MSFT")[0]["close"] == 9
    assert storage.get_prices("NVDA") == []


def test_null_values_are_stored_as_null(db):
    storage.upsert_prices("DCOILWTICO", "fred", [row("2026-01-02", 70, open=None, volume=None)])
    got = storage.get_prices("DCOILWTICO")[0]
    assert got["open"] is None and got["volume"] is None and got["close"] == 70


# ---------- stored_summary ----------

def test_stored_summary_has_one_row_per_symbol(db):
    storage.upsert_prices("MSFT", "yahoo", [row("2026-01-02"), row("2026-01-05")])
    storage.upsert_prices("AAPL", "yahoo", [row("2026-01-03")])
    summary = storage.stored_summary()
    assert [(s["symbol"], s["rows"], s["first_date"], s["last_date"]) for s in summary] == [
        ("AAPL", 1, "2026-01-03", "2026-01-03"),
        ("MSFT", 2, "2026-01-02", "2026-01-05"),
    ]


def test_stored_summary_empty(db):
    assert storage.stored_summary() == []


# ---------- watchlist ----------

def test_watchlist_add_is_idempotent_and_sorted(db):
    for s in ("MSFT", "AAPL", "AAPL"):
        storage.add_to_watchlist(s)
    assert storage.list_watchlist() == ["AAPL", "MSFT"]


def test_watchlist_remove(db):
    storage.add_to_watchlist("AAPL")
    storage.remove_from_watchlist("AAPL")
    storage.remove_from_watchlist("AAPL")  # removing a missing symbol is harmless
    assert storage.list_watchlist() == []
