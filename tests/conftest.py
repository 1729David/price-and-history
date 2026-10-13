import pytest

import app as app_module
import storage


@pytest.fixture
def db(tmp_path, monkeypatch):
    """Throwaway database; never touches data/prices.db."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    storage.init_db()


@pytest.fixture
def client(db):
    return app_module.app.test_client()
