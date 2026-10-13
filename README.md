# Price & History

Retrieve and store market indicators (S&P 500, gold, oil) and daily price history for stocks you choose.
Python (Flask + SQLite) backend, vanilla HTML/CSS/JS frontend.

## Setup

Dependencies are managed with [uv](https://docs.astral.sh/uv/).

```powershell
cd ~\Documents\repos\price-and-history
uv run backend\app.py
```

```bash
cd ~/Documents/repos/price-and-history
uv run backend/app.py
```

`uv run` creates `.venv` and installs dependencies from `uv.lock` automatically on first run.

Open http://127.0.0.1:5000.

## Layout

```
backend/
  app.py       Flask API, also serves the frontend
  sources.py   Indicator list plus Yahoo Finance and FRED fetchers
  storage.py   SQLite schema and queries (data/prices.db)
frontend/
  index.html, styles.css, app.js
data/          SQLite database lives here (git-ignored)
```

## Data sources

Yahoo Finance via `yfinance` supplies stocks, the S&P 500 (`^GSPC`), gold futures (`GC=F`), WTI (`CL=F`)
and Brent (`BZ=F`) futures. FRED's public CSV endpoint supplies daily WTI (`DCOILWTICO`) and Brent
(`DCOILBRENTEU`) spot prices; no API key is needed. To add an indicator, add an entry to `INDICATORS`
in `backend/sources.py` and it appears in the dropdown.

Prices are stored one row per symbol per day. Fetching an overlapping range updates existing days
rather than duplicating them.

## News (from custom-agent)

News for a stock comes from the sibling [custom-agent](../custom-agent) REST API; this app only asks for
it and stores the result. Start the agent first:

```bash
cd ../custom-agent
API_KEY=changeme uv run uvicorn server:get_app --factory --host 127.0.0.1 --port 8000
```

| Env var | Default | Purpose |
|---|---|---|
| `CUSTOM_AGENT_URL` | `http://127.0.0.1:8000` | Where the agent listens |
| `CUSTOM_AGENT_API_KEY` | unset | Sent as `X-API-Key` |

In the UI, pick a stored stock and press **Fetch news**. A job takes minutes; the page polls it and shows
progress. The first poll that sees it finished copies the overview and articles into `data/prices.db`
(`news_jobs`, `news_overviews`, `news_articles`). Articles are keyed by symbol and URL, so a repeat job
updates rows instead of duplicating them. If the browser is closed mid-job, the next `GET /api/news/<symbol>`
stores whatever finished meanwhile. Stored news is served even when the agent is offline. A job the agent
pauses (for example Ollama is down) is reported with its error; resume it on the agent and fetch again.

The agent only accepts stock tickers, so indicators like `^GSPC` are rejected with an error.

## API

| Method | Path | Body / query |
|---|---|---|
| GET | `/api/indicators` | |
| POST | `/api/indicators/fetch` | `{keys: ["sp500"], start, end}` |
| GET | `/api/watchlist` | |
| POST | `/api/watchlist` | `{symbols: "AAPL, MSFT"}` |
| DELETE | `/api/watchlist/<symbol>` | |
| POST | `/api/stocks/fetch` | `{start, end, symbols?}` (defaults to watchlist) |
| GET | `/api/prices` | `?symbol=^GSPC&start=&end=` |
| GET | `/api/stored` | |
| POST | `/api/news/<symbol>` | `{days?, max_articles?, refresh?}`; queues an agent job, returns `{job_id, status}` |
| GET | `/api/news/jobs/<job_id>` | status and progress; stores the result once done |
| GET | `/api/news/<symbol>` | stored overview and articles |

Dates are `YYYY-MM-DD`; the default range is the last 365 days.

## Tests

```bash
uv run --group dev pytest
```

They stub custom-agent and use a temporary database.
