"""News integration: custom-agent is stubbed, so no network and no running agent needed."""
import pytest

import agent_client
import storage


def make_job(status="done", job_id="job1", articles=None, error=None, sentiment=None):
    job = {"id": job_id, "status": status, "error": error, "progress": {"stage": status, "counts": {}}}
    if status == "done":
        job["result"] = {
            "ticker": "AAPL",
            "generated_at": "2026-10-12T10:00:00Z",
            "overview": "Apple had a quiet month.",
            **({"sentiment": sentiment} if sentiment else {}),
            "articles": articles if articles is not None else [{
                "article_id": 1, "title": "iPhone sales", "source": "Reuters",
                "url": "https://example.com/a", "published_at": "2026-10-10",
                "retrieved_at": "2026-10-12", "summary": "Sales rose.",
            }],
            "skipped": [],
        }
    return job


@pytest.fixture
def agent(monkeypatch):
    """Stub of the custom-agent API. Tests set agent.job to what GET /jobs/<id> should return."""
    class Stub:
        job = make_job("queued")
        started = []

    stub = Stub()

    def start(ticker, **kwargs):
        stub.started.append((ticker, kwargs))
        return {"id": stub.job["id"], "status": "queued"}

    monkeypatch.setattr(agent_client, "start_news_job", start)
    monkeypatch.setattr(agent_client, "get_job", lambda job_id: stub.job)
    return stub


def test_start_records_job_and_passes_options(client, agent):
    resp = client.post("/api/news/aapl", json={"days": 7, "max_articles": 5, "refresh": True})
    assert resp.status_code == 202
    assert resp.get_json() == {"job_id": "job1", "status": "queued"}
    assert agent.started == [("AAPL", {"days": 7, "max_articles": 5, "refresh": True})]
    assert storage.get_news_job("job1")["symbol"] == "AAPL"


def test_rejects_bad_ticker(client, agent):
    assert client.post("/api/news/bad%20ticker!").status_code == 400
    assert agent.started == []


def test_running_job_reports_progress_and_stores_nothing(client, agent):
    client.post("/api/news/AAPL")
    agent.job = make_job("running")
    body = client.get("/api/news/jobs/job1").get_json()
    assert body["status"] == "running"
    assert body["progress"]["stage"] == "running"
    assert client.get("/api/news/AAPL").get_json()["articles"] == []


def test_done_job_is_stored_once(client, agent):
    client.post("/api/news/AAPL")
    agent.job = make_job("done")
    assert client.get("/api/news/jobs/job1").get_json()["status"] == "done"

    news = client.get("/api/news/AAPL").get_json()
    assert news["overview"]["overview"] == "Apple had a quiet month."
    assert [a["title"] for a in news["articles"]] == ["iPhone sales"]

    # Stored data no longer needs the agent: even if it is gone, the job reports done.
    agent.job = None
    assert client.get("/api/news/jobs/job1").get_json()["status"] == "done"
    assert storage.pending_news_jobs() == []


def test_listing_stores_jobs_that_finished_unwatched(client, agent):
    client.post("/api/news/AAPL")
    agent.job = make_job("done")
    news = client.get("/api/news/AAPL").get_json()  # no job poll in between
    assert len(news["articles"]) == 1


def test_same_url_in_a_later_job_updates_instead_of_duplicating(client, agent):
    client.post("/api/news/AAPL")
    agent.job = make_job("done")
    client.get("/api/news/jobs/job1")

    agent.job = make_job("done", job_id="job2", articles=[{
        "article_id": 1, "title": "iPhone sales", "url": "https://example.com/a",
        "summary": "Sales rose again.",
    }])
    client.post("/api/news/AAPL")
    client.get("/api/news/jobs/job2")

    articles = client.get("/api/news/AAPL").get_json()["articles"]
    assert len(articles) == 1
    assert articles[0]["summary"] == "Sales rose again."


def test_failed_job_reports_error_and_is_not_polled_again(client, agent):
    client.post("/api/news/AAPL")
    agent.job = make_job("failed", error="unknown model")
    body = client.get("/api/news/jobs/job1").get_json()
    assert body["status"] == "failed" and body["error"] == "unknown model"
    assert storage.pending_news_jobs() == []


def test_unknown_job_is_404(client, agent):
    assert client.get("/api/news/jobs/nope").status_code == 404


def test_agent_offline_returns_502_on_start_but_serves_stored_news(client, agent, monkeypatch):
    client.post("/api/news/AAPL")
    agent.job = make_job("done")
    client.get("/api/news/jobs/job1")

    def down(*args, **kwargs):
        raise agent_client.AgentError("Can't reach custom-agent")

    monkeypatch.setattr(agent_client, "start_news_job", down)
    monkeypatch.setattr(agent_client, "get_job", down)
    resp = client.post("/api/news/AAPL")
    assert resp.status_code == 502 and "custom-agent" in resp.get_json()["error"]

    # stored news is still served while the agent is down
    assert len(client.get("/api/news/AAPL").get_json()["articles"]) == 1


def test_pending_job_survives_agent_outage_on_listing(client, agent, monkeypatch):
    client.post("/api/news/AAPL")

    def down(job_id):
        raise agent_client.AgentError("down")

    monkeypatch.setattr(agent_client, "get_job", down)
    assert client.get("/api/news/AAPL").status_code == 200
    assert len(storage.pending_news_jobs()) == 1


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.ok = status < 400
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.text = str(self._payload)

    def json(self):
        return self._payload


def test_client_sends_api_key_and_maps_errors(monkeypatch):
    seen = {}

    def fake_request(method, url, headers, timeout, **kwargs):
        seen.update(method=method, url=url, headers=headers, json=kwargs.get("json"))
        return FakeResponse(422, {"detail": "invalid ticker"})

    monkeypatch.setattr(agent_client, "API_KEY", "secret")
    monkeypatch.setattr(agent_client.requests, "request", fake_request)
    with pytest.raises(agent_client.AgentError, match="422: invalid ticker"):
        agent_client.start_news_job("AAPL", days=3)
    assert seen["headers"] == {"X-API-Key": "secret"}
    assert seen["url"].endswith("/news/AAPL")
    assert seen["json"] == {"days": 3, "max_articles": 15, "refresh": False}


# ---------- batch queueing, job list, per-job data ----------

@pytest.fixture
def multi_agent(monkeypatch):
    """Agent stub that hands out a distinct job per ticker and serves whatever is in `jobs`."""
    class Stub:
        jobs = {}
        rejected = set()

    stub = Stub()

    def start(ticker, **kwargs):
        if ticker in stub.rejected:
            raise agent_client.AgentError("custom-agent returned 422: invalid ticker")
        job = make_job("queued", job_id=f"job-{ticker}")
        stub.jobs[job["id"]] = job
        return {"id": job["id"], "status": "queued"}

    monkeypatch.setattr(agent_client, "start_news_job", start)
    monkeypatch.setattr(agent_client, "get_job", lambda job_id: stub.jobs[job_id])
    return stub


def finish(stub, ticker, **kwargs):
    stub.jobs[f"job-{ticker}"] = make_job("done", job_id=f"job-{ticker}", **kwargs)


def test_batch_queues_one_job_per_distinct_ticker(client, multi_agent):
    resp = client.post("/api/news", json={"symbols": "aapl, MSFT;aapl  nvda"})
    assert resp.status_code == 202
    results = resp.get_json()["results"]
    assert [(r["symbol"], r["job_id"], r["ok"]) for r in results] == [
        ("AAPL", "job-AAPL", True), ("MSFT", "job-MSFT", True), ("NVDA", "job-NVDA", True)]
    assert {j["symbol"] for j in storage.list_news_jobs()} == {"AAPL", "MSFT", "NVDA"}


def test_batch_reports_a_rejected_ticker_without_failing_the_rest(client, multi_agent):
    multi_agent.rejected.add("^GSPC")
    results = client.post("/api/news", json={"symbols": ["AAPL", "^GSPC"]}).get_json()["results"]
    assert [r["ok"] for r in results] == [True, False]
    assert "422" in results[1]["error"]
    assert [j["symbol"] for j in storage.list_news_jobs()] == ["AAPL"]


def test_batch_validates_input(client, multi_agent):
    assert client.post("/api/news", json={"symbols": "  "}).status_code == 400
    assert client.post("/api/news", json={"symbols": "AAPL, bad!"}).status_code == 400
    too_many = ",".join(f"T{i}" for i in range(21))
    assert client.post("/api/news", json={"symbols": too_many}).status_code == 400
    assert storage.list_news_jobs() == []  # a bad ticker anywhere queues nothing


def test_job_list_shows_status_progress_and_article_counts(client, multi_agent):
    client.post("/api/news", json={"symbols": "AAPL, MSFT"})
    finish(multi_agent, "AAPL")
    multi_agent.jobs["job-MSFT"] = make_job("running", job_id="job-MSFT")

    jobs = {j["symbol"]: j for j in client.get("/api/news/jobs").get_json()}
    assert jobs["AAPL"]["status"] == "done" and jobs["AAPL"]["article_count"] == 1
    assert jobs["MSFT"]["status"] == "running" and jobs["MSFT"]["article_count"] is None
    assert jobs["MSFT"]["progress"]["stage"] == "running"
    # Once stored, a job is no longer asked about, so a later listing has no progress for it.
    again = {j["symbol"]: j for j in client.get("/api/news/jobs").get_json()}
    assert again["AAPL"]["progress"] is None


def test_job_list_is_newest_first_and_survives_an_offline_agent(client, multi_agent, monkeypatch):
    client.post("/api/news", json={"symbols": "AAPL, MSFT"})

    def down(job_id):
        raise agent_client.AgentError("down")

    monkeypatch.setattr(agent_client, "get_job", down)
    resp = client.get("/api/news/jobs")
    assert resp.status_code == 200
    assert [j["symbol"] for j in resp.get_json()] == ["MSFT", "AAPL"]


def test_job_result_returns_everything_the_job_retrieved(client, multi_agent):
    client.post("/api/news", json={"symbols": "AAPL"})
    finish(multi_agent, "AAPL")
    multi_agent.jobs["job-AAPL"]["result"]["skipped"] = [{"url": "https://example.com/x", "reason": "not relevant"}]
    client.get("/api/news/jobs")  # stores it

    body = client.get("/api/news/jobs/job-AAPL/result").get_json()
    assert body["symbol"] == "AAPL" and body["overview"] == "Apple had a quiet month."
    assert [a["title"] for a in body["articles"]] == ["iPhone sales"]
    assert body["skipped"][0]["reason"] == "not relevant"


def test_job_result_keeps_each_jobs_own_articles(client, multi_agent):
    """A later job re-fetching the same URL must not rewrite what the earlier job retrieved."""
    client.post("/api/news", json={"symbols": "AAPL"})
    finish(multi_agent, "AAPL")
    client.get("/api/news/jobs")

    multi_agent.jobs["job-AAPL2"] = make_job("done", job_id="job-AAPL2", articles=[{
        "article_id": 1, "title": "iPhone sales", "url": "https://example.com/a", "summary": "Newer summary."}])
    storage.record_news_job("job-AAPL2", "AAPL", "queued")
    client.get("/api/news/jobs")

    first = client.get("/api/news/jobs/job-AAPL/result").get_json()
    assert first["articles"][0]["summary"] == "Sales rose."
    second = client.get("/api/news/jobs/job-AAPL2/result").get_json()
    assert second["articles"][0]["summary"] == "Newer summary."


def test_job_result_for_unfinished_or_unknown_job(client, multi_agent):
    client.post("/api/news", json={"symbols": "AAPL"})
    assert client.get("/api/news/jobs/job-AAPL/result").status_code == 409
    assert client.get("/api/news/jobs/nope/result").status_code == 404


def test_job_stored_before_snapshots_existed_is_recovered_from_the_agent(client, multi_agent):
    client.post("/api/news", json={"symbols": "AAPL"})
    finish(multi_agent, "AAPL")
    client.get("/api/news/jobs")
    with storage.connect() as conn:
        conn.execute("DELETE FROM news_job_results")  # as if stored by the earlier version

    listed = client.get("/api/news/jobs").get_json()[0]
    assert listed["article_count"] == 1  # counted from the article rows

    body = client.get("/api/news/jobs/job-AAPL/result").get_json()
    assert body["overview"] == "Apple had a quiet month."
    assert storage.get_job_result("job-AAPL") is not None  # saved for next time


# ---------- sentiment: the agent's label, which the user can change ----------

def test_job_list_and_result_carry_the_agents_sentiment(client, multi_agent):
    client.post("/api/news", json={"symbols": "AAPL, MSFT"})
    finish(multi_agent, "AAPL", sentiment="negative")
    finish(multi_agent, "MSFT")  # a result from before the agent labeled sentiment

    jobs = {j["symbol"]: j for j in client.get("/api/news/jobs").get_json()}
    assert (jobs["AAPL"]["sentiment"], jobs["AAPL"]["sentiment_source"]) == ("negative", "agent")
    assert (jobs["MSFT"]["sentiment"], jobs["MSFT"]["sentiment_source"]) == (None, None)
    assert "sentiment_override" not in jobs["AAPL"]

    body = client.get("/api/news/jobs/job-AAPL/result").get_json()
    assert (body["sentiment"], body["agent_sentiment"], body["sentiment_source"]) == ("negative", "negative", "agent")


def test_user_can_relabel_a_job_and_go_back_to_the_agents_label(client, multi_agent):
    client.post("/api/news", json={"symbols": "AAPL"})
    finish(multi_agent, "AAPL", sentiment="negative")
    client.get("/api/news/jobs")

    resp = client.put("/api/news/jobs/job-AAPL/sentiment", json={"sentiment": "mixed"})
    assert resp.status_code == 200
    assert resp.get_json() == {"job_id": "job-AAPL", "sentiment": "mixed",
                               "agent_sentiment": "negative", "sentiment_source": "user"}
    listed = client.get("/api/news/jobs").get_json()[0]
    assert (listed["sentiment"], listed["sentiment_source"]) == ("mixed", "user")
    assert client.get("/api/news/jobs/job-AAPL/result").get_json()["sentiment"] == "mixed"

    reset = client.put("/api/news/jobs/job-AAPL/sentiment", json={"sentiment": None}).get_json()
    assert (reset["sentiment"], reset["sentiment_source"]) == ("negative", "agent")


def test_relabel_validates_input(client, multi_agent):
    client.post("/api/news", json={"symbols": "AAPL"})
    url = "/api/news/jobs/job-AAPL/sentiment"
    assert client.put(url, json={"sentiment": "bullish"}).status_code == 400
    assert client.put(url, json={}).status_code == 400
    assert client.put("/api/news/jobs/nope/sentiment", json={"sentiment": "mixed"}).status_code == 404


def test_user_label_works_for_a_job_the_agent_never_labeled(client, multi_agent):
    client.post("/api/news", json={"symbols": "AAPL"})
    finish(multi_agent, "AAPL")
    client.get("/api/news/jobs")
    body = client.put("/api/news/jobs/job-AAPL/sentiment", json={"sentiment": "positive"}).get_json()
    assert (body["sentiment"], body["agent_sentiment"], body["sentiment_source"]) == ("positive", None, "user")


def test_database_from_before_sentiment_labels_is_upgraded(tmp_path, monkeypatch):
    import sqlite3
    path = tmp_path / "old.db"
    with sqlite3.connect(path) as conn:
        conn.execute("""CREATE TABLE news_jobs (job_id TEXT PRIMARY KEY, symbol TEXT NOT NULL,
            status TEXT NOT NULL, error TEXT, requested_at TEXT NOT NULL DEFAULT (datetime('now')),
            stored INTEGER NOT NULL DEFAULT 0)""")
        conn.execute("INSERT INTO news_jobs (job_id, symbol, status) VALUES ('old', 'AAPL', 'queued')")
    monkeypatch.setattr(storage, "DB_PATH", path)
    storage.init_db()
    storage.init_db()  # running again is harmless
    storage.set_sentiment_override("old", "mixed")
    assert storage.list_news_jobs()[0]["sentiment"] == "mixed"
