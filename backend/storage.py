"""SQLite storage for price history and the user's stock watchlist."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import date
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "prices.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS prices (
    symbol     TEXT NOT NULL,
    date       TEXT NOT NULL,
    open       REAL,
    high       REAL,
    low        REAL,
    close      REAL,
    adj_close  REAL,
    volume     REAL,
    source     TEXT NOT NULL,
    fetched_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (symbol, date)
);

CREATE TABLE IF NOT EXISTS watchlist (
    symbol   TEXT PRIMARY KEY,
    added_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS fetch_coverage (
    symbol     TEXT NOT NULL,
    source     TEXT NOT NULL,
    start_date TEXT NOT NULL,
    end_date   TEXT NOT NULL,
    PRIMARY KEY (symbol, source, start_date, end_date)
);

-- News comes from custom-agent. A job is tracked here from the moment we queue it; its
-- result is copied in once, when the agent reports it done.
CREATE TABLE IF NOT EXISTS news_jobs (
    job_id       TEXT PRIMARY KEY,
    symbol       TEXT NOT NULL,
    status       TEXT NOT NULL,
    error        TEXT,
    requested_at TEXT NOT NULL DEFAULT (datetime('now')),
    stored       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS news_overviews (
    job_id       TEXT PRIMARY KEY,
    symbol       TEXT NOT NULL,
    generated_at TEXT NOT NULL,
    overview     TEXT NOT NULL
);

-- The agent's complete result for each finished job (overview, articles, skipped), as returned.
-- news_articles below keeps only the latest version of each article per symbol, so this is
-- what lets us show exactly what one job retrieved.
CREATE TABLE IF NOT EXISTS news_job_results (
    job_id      TEXT PRIMARY KEY,
    result_json TEXT NOT NULL
);

-- One row per article per symbol. The agent reuses articles across jobs, so the same URL
-- arriving again updates the row instead of duplicating it.
CREATE TABLE IF NOT EXISTS news_articles (
    symbol       TEXT NOT NULL,
    url          TEXT NOT NULL,
    article_id   INTEGER,
    title        TEXT,
    source       TEXT,
    published_at TEXT,
    retrieved_at TEXT,
    summary      TEXT NOT NULL,
    job_id       TEXT NOT NULL,
    PRIMARY KEY (symbol, url)
);
"""


@contextmanager
def connect():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)


def missing_ranges(symbol: str, source: str, start: date, end: date) -> list[tuple[date, date]]:
    """Sub-intervals of [start, end) not yet covered by a prior fetch for this symbol/source.

    Coverage tracks calendar ranges we've already asked the source about (not which dates
    have rows), since weekends/holidays legitimately have no row inside a fully-fetched range.
    """
    with connect() as conn:
        covered = [
            (date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"]))
            for r in conn.execute(
                "SELECT start_date, end_date FROM fetch_coverage WHERE symbol = ? AND source = ? ORDER BY start_date",
                (symbol, source),
            )
        ]

    missing = []
    cursor = start
    for cs, ce in covered:
        if ce <= cursor:
            continue
        if cs >= end:
            break
        if cs > cursor:
            missing.append((cursor, min(cs, end)))
        cursor = max(cursor, ce)
        if cursor >= end:
            break
    if cursor < end:
        missing.append((cursor, end))
    return missing


def add_coverage(symbol: str, source: str, start: date, end: date) -> None:
    """Record [start, end) as fetched, merging with any overlapping/adjacent coverage."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT start_date, end_date FROM fetch_coverage WHERE symbol = ? AND source = ?",
            (symbol, source),
        ).fetchall()
        intervals = sorted(
            [(date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"])) for r in rows]
            + [(start, end)]
        )

        merged: list[list[date]] = []
        for cs, ce in intervals:
            if merged and cs <= merged[-1][1]:
                merged[-1][1] = max(merged[-1][1], ce)
            else:
                merged.append([cs, ce])

        conn.execute("DELETE FROM fetch_coverage WHERE symbol = ? AND source = ?", (symbol, source))
        conn.executemany(
            "INSERT INTO fetch_coverage (symbol, source, start_date, end_date) VALUES (?, ?, ?, ?)",
            [(symbol, source, cs.isoformat(), ce.isoformat()) for cs, ce in merged],
        )


def upsert_prices(symbol: str, source: str, rows: list[dict]) -> int:
    """Insert or replace rows. Re-fetching a range refreshes existing days."""
    with connect() as conn:
        conn.executemany(
            """
            INSERT INTO prices (symbol, date, open, high, low, close, adj_close, volume, source)
            VALUES (:symbol, :date, :open, :high, :low, :close, :adj_close, :volume, :source)
            ON CONFLICT(symbol, date) DO UPDATE SET
                open=excluded.open, high=excluded.high, low=excluded.low,
                close=excluded.close, adj_close=excluded.adj_close,
                volume=excluded.volume, source=excluded.source,
                fetched_at=datetime('now')
            """,
            [{**r, "symbol": symbol, "source": source} for r in rows],
        )
    return len(rows)


def get_prices(symbol: str, start: str | None = None, end: str | None = None) -> list[dict]:
    query = "SELECT date, open, high, low, close, adj_close, volume FROM prices WHERE symbol = ?"
    params: list = [symbol]
    if start:
        query += " AND date >= ?"
        params.append(start)
    if end:
        query += " AND date <= ?"
        params.append(end)
    query += " ORDER BY date"
    with connect() as conn:
        return [dict(r) for r in conn.execute(query, params)]


def stored_summary() -> list[dict]:
    """One row per symbol: how much history is on disk."""
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            """
            SELECT symbol, source, COUNT(*) AS rows, MIN(date) AS first_date,
                   MAX(date) AS last_date, MAX(fetched_at) AS last_fetched
            FROM prices GROUP BY symbol ORDER BY symbol
            """
        )]


def list_watchlist() -> list[str]:
    with connect() as conn:
        return [r["symbol"] for r in conn.execute("SELECT symbol FROM watchlist ORDER BY symbol")]


def add_to_watchlist(symbol: str) -> None:
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO watchlist (symbol) VALUES (?)", (symbol,))


def remove_from_watchlist(symbol: str) -> None:
    with connect() as conn:
        conn.execute("DELETE FROM watchlist WHERE symbol = ?", (symbol,))


# ---------- News (from custom-agent) ----------

def record_news_job(job_id: str, symbol: str, status: str) -> None:
    """Track a queued job. Re-queueing an identical job returns the same id, which is harmless."""
    with connect() as conn:
        conn.execute(
            "INSERT OR IGNORE INTO news_jobs (job_id, symbol, status) VALUES (?, ?, ?)",
            (job_id, symbol, status),
        )


def get_news_job(job_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT * FROM news_jobs WHERE job_id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def pending_news_jobs(symbol: str | None = None) -> list[dict]:
    """Jobs whose results we haven't stored yet (and that haven't failed)."""
    query = "SELECT * FROM news_jobs WHERE stored = 0 AND status != 'failed'"
    params: list = []
    if symbol:
        query += " AND symbol = ?"
        params.append(symbol)
    with connect() as conn:
        return [dict(r) for r in conn.execute(query + " ORDER BY requested_at", params)]


def update_news_job(job_id: str, status: str, error: str | None) -> None:
    with connect() as conn:
        conn.execute("UPDATE news_jobs SET status = ?, error = ? WHERE job_id = ?", (status, error, job_id))


def store_news_result(job_id: str, symbol: str, result: dict) -> int:
    """Save a finished job's overview and articles in one transaction. Returns the article count."""
    articles = result.get("articles") or []
    with connect() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO news_overviews (job_id, symbol, generated_at, overview)
            VALUES (?, ?, ?, ?)
            """,
            (job_id, symbol, result["generated_at"], result["overview"]),
        )
        conn.executemany(
            """
            INSERT INTO news_articles
                (symbol, url, article_id, title, source, published_at, retrieved_at, summary, job_id)
            VALUES (:symbol, :url, :article_id, :title, :source, :published_at, :retrieved_at, :summary, :job_id)
            ON CONFLICT(symbol, url) DO UPDATE SET
                article_id=excluded.article_id, title=excluded.title, source=excluded.source,
                published_at=excluded.published_at, retrieved_at=excluded.retrieved_at,
                summary=excluded.summary, job_id=excluded.job_id
            """,
            [
                {"symbol": symbol, "job_id": job_id, "article_id": None, "title": None,
                 "source": None, "published_at": None, "retrieved_at": None, **a}
                for a in articles
            ],
        )
        conn.execute(
            "INSERT OR REPLACE INTO news_job_results (job_id, result_json) VALUES (?, ?)",
            (job_id, json.dumps(result)),
        )
        conn.execute(
            "UPDATE news_jobs SET status = 'done', error = NULL, stored = 1 WHERE job_id = ?", (job_id,)
        )
    return len(articles)


def save_job_result(job_id: str, result: dict) -> None:
    """Keep the full result of a job stored before snapshots existed."""
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO news_job_results (job_id, result_json) VALUES (?, ?)",
            (job_id, json.dumps(result)),
        )


def get_job_result(job_id: str) -> dict | None:
    with connect() as conn:
        row = conn.execute("SELECT result_json FROM news_job_results WHERE job_id = ?", (job_id,)).fetchone()
    return json.loads(row["result_json"]) if row else None


def list_news_jobs() -> list[dict]:
    """Every job we have queued, newest first, with how many articles a finished one kept."""
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            """
            SELECT j.job_id, j.symbol, j.status, j.error, j.requested_at, j.stored,
                   CASE WHEN j.stored = 0 THEN NULL
                        WHEN r.result_json IS NOT NULL
                            THEN json_array_length(json_extract(r.result_json, '$.articles'))
                        ELSE (SELECT COUNT(*) FROM news_articles a WHERE a.job_id = j.job_id)
                   END AS article_count
            FROM news_jobs j LEFT JOIN news_job_results r ON r.job_id = j.job_id
            ORDER BY j.requested_at DESC, j.rowid DESC
            """
        )]


def get_news(symbol: str) -> dict:
    """Latest overview plus every stored article for a symbol, newest first."""
    with connect() as conn:
        overview = conn.execute(
            "SELECT job_id, generated_at, overview FROM news_overviews WHERE symbol = ? "
            "ORDER BY generated_at DESC LIMIT 1",
            (symbol,),
        ).fetchone()
        articles = conn.execute(
            "SELECT article_id, title, source, url, published_at, retrieved_at, summary "
            "FROM news_articles WHERE symbol = ? ORDER BY COALESCE(published_at, retrieved_at) DESC",
            (symbol,),
        ).fetchall()
    return {
        "symbol": symbol,
        "overview": dict(overview) if overview else None,
        "articles": [dict(a) for a in articles],
    }
