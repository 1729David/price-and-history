"""Flask API for Price & History. Also serves the vanilla frontend.

Run from the project root:
    python backend/app.py
then open http://127.0.0.1:5000
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory

import agent_client
import sources
import storage

FRONTEND_DIR = Path(__file__).resolve().parent.parent / "frontend"
TICKER_RE = re.compile(r"^[A-Z0-9.\-^=]{1,15}$")

app = Flask(__name__, static_folder=None)
storage.init_db()


def _parse_date(value: str | None, default: date) -> date:
    if not value:
        return default
    return datetime.strptime(value, "%Y-%m-%d").date()


def _date_range(payload: dict) -> tuple[date, date]:
    end = _parse_date(payload.get("end"), date.today())
    start = _parse_date(payload.get("start"), end - timedelta(days=365))
    if start >= end:
        raise ValueError("Start date must be before end date.")
    # yfinance treats `end` as exclusive, so include the chosen end day.
    return start, end + timedelta(days=1)


def _clean_ticker(raw: str) -> str:
    symbol = (raw or "").strip().upper()
    if not TICKER_RE.match(symbol):
        raise ValueError(f"'{raw}' doesn't look like a ticker symbol.")
    return symbol


def _fetch_and_store(source: str, symbol: str, start: date, end: date) -> dict:
    try:
        missing = storage.missing_ranges(symbol, source, start, end)
        fresh_dates: set[str] = set()
        for m_start, m_end in missing:
            rows = sources.fetch(source, symbol, m_start, m_end)
            storage.upsert_prices(symbol, source, rows)
            fresh_dates.update(r["date"] for r in rows)
            storage.add_coverage(symbol, source, m_start, m_end)

        all_rows = storage.get_prices(symbol, start.isoformat(), (end - timedelta(days=1)).isoformat())
        return {
            "symbol": symbol,
            "ok": True,
            "rows": len(all_rows),
            "fetched": len(fresh_dates),
            "cached": len(all_rows) - len(fresh_dates),
            "fresh_dates": sorted(fresh_dates),
        }
    except Exception as exc:  # report per-symbol failures instead of failing the batch
        return {"symbol": symbol, "ok": False, "error": str(exc)}


# ---------- Frontend ----------

@app.get("/")
def index():
    return send_from_directory(FRONTEND_DIR, "index.html")


@app.get("/<path:filename>")
def static_files(filename):
    return send_from_directory(FRONTEND_DIR, filename)


# ---------- API ----------

@app.errorhandler(ValueError)
def bad_request(exc):
    return jsonify({"error": str(exc)}), 400


@app.errorhandler(agent_client.AgentError)
def agent_unavailable(exc):
    return jsonify({"error": str(exc)}), 502


@app.get("/api/indicators")
def list_indicators():
    return jsonify([{"key": k, **v} for k, v in sources.INDICATORS.items()])


@app.post("/api/indicators/fetch")
def fetch_indicators():
    payload = request.get_json(silent=True) or {}
    keys = payload.get("keys") or []
    if not keys:
        raise ValueError("Pick at least one indicator.")
    unknown = [k for k in keys if k not in sources.INDICATORS]
    if unknown:
        raise ValueError(f"Unknown indicator(s): {', '.join(unknown)}")

    start, end = _date_range(payload)
    results = []
    for key in keys:
        meta = sources.INDICATORS[key]
        results.append({"key": key, **_fetch_and_store(meta["source"], meta["symbol"], start, end)})
    return jsonify({"results": results})


@app.get("/api/watchlist")
def get_watchlist():
    return jsonify(storage.list_watchlist())


@app.post("/api/watchlist")
def add_watchlist():
    payload = request.get_json(silent=True) or {}
    # Accept "AAPL, msft nvda" style input.
    raw = payload.get("symbols", "")
    parts = raw if isinstance(raw, list) else re.split(r"[\s,;]+", raw)
    symbols = [_clean_ticker(p) for p in parts if p.strip()]
    if not symbols:
        raise ValueError("Enter at least one ticker.")
    for s in symbols:
        storage.add_to_watchlist(s)
    return jsonify(storage.list_watchlist())


@app.delete("/api/watchlist/<symbol>")
def delete_watchlist(symbol):
    storage.remove_from_watchlist(_clean_ticker(symbol))
    return jsonify(storage.list_watchlist())


@app.post("/api/stocks/fetch")
def fetch_stocks():
    payload = request.get_json(silent=True) or {}
    symbols = [_clean_ticker(s) for s in (payload.get("symbols") or storage.list_watchlist())]
    if not symbols:
        raise ValueError("Your watchlist is empty. Add a ticker first.")
    start, end = _date_range(payload)
    return jsonify({"results": [_fetch_and_store("yahoo", s, start, end) for s in symbols]})


@app.get("/api/prices")
def get_prices():
    symbol = request.args.get("symbol", "")
    if not symbol:
        raise ValueError("Missing symbol.")
    return jsonify({
        "symbol": symbol,
        "rows": storage.get_prices(symbol, request.args.get("start"), request.args.get("end")),
    })


@app.get("/api/stored")
def stored():
    return jsonify(storage.stored_summary())


# ---------- News (custom-agent does the work, we store the result) ----------

def _sync_news_job(job: dict) -> dict:
    """Ask custom-agent about a job; when it's done, copy its result into our database once."""
    remote = agent_client.get_job(job["job_id"])
    status = remote["status"]
    if status == "done" and not job["stored"]:
        storage.store_news_result(job["job_id"], job["symbol"], remote["result"])
    else:
        storage.update_news_job(job["job_id"], status, remote.get("error"))
    return remote


def _sync_pending(symbol: str | None = None) -> dict[str, dict]:
    """Refresh unfinished jobs from the agent. Returns each one's latest agent view by job id."""
    seen = {}
    for job in storage.pending_news_jobs(symbol):
        try:
            seen[job["job_id"]] = _sync_news_job(job)
        except agent_client.AgentError:
            pass  # agent offline: stored data is still served, jobs are retried next time
    return seen


MAX_TICKERS_PER_REQUEST = 20


def _queue_news(symbol: str, payload: dict) -> dict:
    job = agent_client.start_news_job(
        symbol,
        days=int(payload.get("days", 30)),
        max_articles=int(payload.get("max_articles", 15)),
        refresh=bool(payload.get("refresh", False)),
    )
    storage.record_news_job(job["id"], symbol, job["status"])
    return {"symbol": symbol, "job_id": job["id"], "status": job["status"]}


@app.post("/api/news/<symbol>")
def start_news(symbol):
    symbol = _clean_ticker(symbol)
    queued = _queue_news(symbol, request.get_json(silent=True) or {})
    return jsonify({"job_id": queued["job_id"], "status": queued["status"]}), 202


@app.post("/api/news")
def start_news_batch():
    """Queue one job per ticker. A ticker the agent rejects is reported without failing the rest."""
    payload = request.get_json(silent=True) or {}
    raw = payload.get("symbols", "")
    parts = raw if isinstance(raw, list) else re.split(r"[\s,;]+", raw)
    symbols = list(dict.fromkeys(_clean_ticker(p) for p in parts if p.strip()))
    if not symbols:
        raise ValueError("Enter at least one ticker.")
    if len(symbols) > MAX_TICKERS_PER_REQUEST:
        raise ValueError(f"Queue at most {MAX_TICKERS_PER_REQUEST} tickers at a time.")

    results = []
    for symbol in symbols:
        try:
            results.append({"ok": True, **_queue_news(symbol, payload)})
        except agent_client.AgentError as exc:
            results.append({"ok": False, "symbol": symbol, "error": str(exc)})
    return jsonify({"results": results}), 202


@app.get("/api/news/jobs")
def list_news_jobs():
    """All queued jobs, newest first. Unfinished ones are refreshed (and stored if done) first."""
    live = _sync_pending()
    jobs = storage.list_news_jobs()
    for job in jobs:
        job["progress"] = live.get(job["job_id"], {}).get("progress")
    return jsonify(jobs)


@app.get("/api/news/jobs/<job_id>/result")
def news_job_result(job_id):
    """Everything one finished job retrieved: overview, kept articles and skipped ones."""
    job = storage.get_news_job(job_id)
    if job is None:
        return jsonify({"error": "Unknown news job."}), 404
    if not job["stored"]:
        return jsonify({"error": "This job hasn't finished yet."}), 409
    result = storage.get_job_result(job_id)
    if result is None:
        # Stored before full results were kept; the agent still has it.
        result = agent_client.get_job(job_id)["result"]
        storage.save_job_result(job_id, result)
    return jsonify({"job_id": job_id, "symbol": job["symbol"], **result})


@app.get("/api/news/jobs/<job_id>")
def news_job(job_id):
    """Progress of a job. The first call that sees it finished also stores the result."""
    job = storage.get_news_job(job_id)
    if job is None:
        return jsonify({"error": "Unknown news job."}), 404
    remote = _sync_news_job(job) if not job["stored"] else {"status": "done", "error": None}
    return jsonify({
        "job_id": job_id,
        "symbol": job["symbol"],
        "status": remote["status"],
        "error": remote.get("error"),
        "progress": remote.get("progress"),
    })


@app.get("/api/news/<symbol>")
def get_news(symbol):
    symbol = _clean_ticker(symbol)
    _sync_pending(symbol)  # picks up jobs that finished while nobody was watching
    return jsonify(storage.get_news(symbol))


if __name__ == "__main__":
    app.run(debug=True)
