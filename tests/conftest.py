import pytest

import app as app_module
import storage


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Flask test client on a throwaway database; never touches data/prices.db."""
    monkeypatch.setattr(storage, "DB_PATH", tmp_path / "prices.db")
    storage.init_db()
    return app_module.app.test_client()
