"""News integration: custom-agent is stubbed, so no network and no running agent needed."""
import pytest

import agent_client
import storage


def make_job(status="done", job_id="job1", articles=None, error=None):
    job = {"id": job_id, "status": status, "error": error, "progress": {"stage": status, "counts": {}}}
    if status == "done":
        job["result"] = {
            "ticker": "AAPL",
            "generated_at": "2026-10-12T10:00:00Z",
            "overview": "Apple had a quiet month.",
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
