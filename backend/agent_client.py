"""Client for the custom-agent REST API (see that repo's README, "REST API").

custom-agent runs news jobs and returns results; it stores nothing for us. This module only
talks HTTP. Persisting results is storage.py's job.

Config (environment):
    CUSTOM_AGENT_URL      default http://127.0.0.1:8000
    CUSTOM_AGENT_API_KEY  sent as X-API-Key when set
"""
from __future__ import annotations

import os

import requests

BASE_URL = os.environ.get("CUSTOM_AGENT_URL", "http://127.0.0.1:8000").rstrip("/")
API_KEY = os.environ.get("CUSTOM_AGENT_API_KEY")
TIMEOUT = 15  # seconds; jobs are async, so every call returns quickly


class AgentError(Exception):
    """custom-agent is unreachable or rejected the request."""


def _request(method: str, path: str, **kwargs) -> dict:
    headers = {"X-API-Key": API_KEY} if API_KEY else {}
    try:
        resp = requests.request(method, BASE_URL + path, headers=headers, timeout=TIMEOUT, **kwargs)
    except requests.RequestException as exc:
        raise AgentError(f"Can't reach custom-agent at {BASE_URL}: {exc}") from exc
    if not resp.ok:
        try:
            detail = resp.json().get("detail", resp.text)
        except ValueError:
            detail = resp.text
        raise AgentError(f"custom-agent returned {resp.status_code}: {detail}")
    return resp.json()


def start_news_job(ticker: str, days: int = 30, max_articles: int = 15, refresh: bool = False) -> dict:
    """Queue a fetch-and-summarize job. An identical queued/running job is reused by the agent."""
    return _request("POST", f"/news/{ticker}", json={
        "days": days, "max_articles": max_articles, "refresh": refresh,
    })


def get_job(job_id: str) -> dict:
    return _request("GET", f"/jobs/{job_id}")


def resume_job(job_id: str) -> dict:
    return _request("POST", f"/jobs/{job_id}/resume")


def cancel_job(job_id: str) -> dict:
    return _request("POST", f"/jobs/{job_id}/cancel")
