"""Shared test fixtures.

Each test module gets its own SQLite file so tests are order-independent and can
run in parallel. The app is constructed through ``create_app()`` after the
environment is patched, which exercises the same startup path as production.
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _configure_environment(database_url: str) -> None:
    os.environ.update(
        {
            "DATABASE_URL": database_url,
            "ENVIRONMENT": "test",
            "SECRET_KEY": "test-secret-key-that-is-long-enough-1234567890",
            "WORKER_SECRET_PEPPER": "test-worker-pepper-1234567890",
            "SECRETS_ENCRYPTION_KEY": "test-encryption-key-1234567890",
            "SCHEDULER_ENABLED": "false",
            "REDIS_URL": "",
            "LOG_LEVEL": "WARNING",
            "LOG_FORMAT": "console",
            "BOOTSTRAP_ADMIN_EMAIL": "",
            "BOOTSTRAP_ADMIN_PASSWORD": "",
        }
    )


# The environment is configured at import time, before pytest collects the test
# modules. Test modules import application packages during collection, and
# ``app.config.settings`` is bound by reference at import time, so reloading the
# config module later would leave already-imported modules holding a different
# settings object than the one fixtures patch. Configuring the environment here
# guarantees a single settings object for the whole run.
_TEMP_DATABASE = tempfile.NamedTemporaryFile(prefix="orchestrator-test-", suffix=".db", delete=False)
_TEMP_DATABASE.close()
TEST_DATABASE_PATH = Path(_TEMP_DATABASE.name)
_configure_environment(f"sqlite:///{TEST_DATABASE_PATH.as_posix()}")


@pytest.fixture(scope="session")
def database_path() -> str:
    yield str(TEST_DATABASE_PATH)
    for suffix in ("", "-wal", "-shm"):
        try:
            os.unlink(str(TEST_DATABASE_PATH) + suffix)
        except OSError:
            pass


@pytest.fixture(scope="session")
def app_module(database_path: str):
    """Build the schema once in the configured test database, then load the app."""
    from app import database as database_module
    from app.models import Base

    Base.metadata.create_all(database_module.engine)

    from app import main as main_module

    return main_module


@pytest.fixture(scope="session")
def app(app_module):
    return app_module.app


@pytest.fixture()
def client(app):
    from fastapi.testclient import TestClient

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture()
def db_session(app_module):
    """A direct session for arranging state the API would not allow."""
    from app.database import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture(autouse=True)
def _reset_rate_limiters():
    """Keep the in-memory rate limiters from leaking across tests."""
    from app.core.rate_limit import auth_limiter, trigger_limiter

    auth_limiter().reset()
    trigger_limiter().reset()
    yield
    auth_limiter().reset()
    trigger_limiter().reset()


@pytest.fixture(autouse=True)
def _reset_resolver_guard():
    """The worker runtime installs a process-wide DNS guard; never let it leak.

    ``Worker.__init__`` calls ``install_resolver_guard`` as a side effect, so
    any test that instantiates a worker would otherwise poison DNS behaviour
    for every test that runs after it in the same process.
    """
    from app.worker import connectors as connector_module

    connector_module.uninstall_resolver_guard()
    yield
    connector_module.uninstall_resolver_guard()


@pytest.fixture()
def account(client):
    """A fresh, isolated account per test."""
    import uuid

    email = f"user-{uuid.uuid4().hex[:10]}@example.com"
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "testpassword123", "display_name": "Test User"},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    return {
        "email": email,
        "password": "testpassword123",
        "token": payload["access_token"],
        "user": payload["user"],
        "headers": {"Authorization": f"Bearer {payload['access_token']}"},
    }


@pytest.fixture()
def other_account(client):
    import uuid

    email = f"other-{uuid.uuid4().hex[:10]}@example.com"
    response = client.post(
        "/api/v1/auth/register",
        json={"email": email, "password": "testpassword123", "display_name": "Other User"},
    )
    assert response.status_code == 201, response.text
    payload = response.json()
    return {"headers": {"Authorization": f"Bearer {payload['access_token']}"}, "user": payload["user"]}


@pytest.fixture()
def worker_client(app):
    """A client plus a worker credential owned by a dedicated account."""
    import uuid

    from fastapi.testclient import TestClient

    from app.core.security import generate_token, hash_token, token_prefix
    from app.database import SessionLocal
    from app.models.worker import Worker

    worker_id = f"test-worker-{uuid.uuid4().hex[:8]}"
    token = generate_token("wrk")
    with SessionLocal() as db:
        db.add(Worker(id=worker_id, name=worker_id, token_hash=hash_token(token), token_prefix=token_prefix(token), max_concurrency=4))
        db.commit()
    with TestClient(app) as test_client:
        test_client.headers.update({"Authorization": f"Bearer {token}"})
        yield {"client": test_client, "worker_id": worker_id, "token": token}


@pytest.fixture()
def workflow_factory(client):
    """Create and optionally publish workflows through the real API."""
    from app.services import demo_data

    def _create(account_headers, definition=None, *, publish=True, name=None):
        payload = definition or demo_data.DEMO_SMOKE_DEFINITION
        payload = {**payload}
        if name:
            payload["name"] = name
        response = client.post("/api/v1/workflows", json=payload, headers=account_headers)
        assert response.status_code == 201, response.text
        workflow = response.json()
        if publish:
            published = client.post(f"/api/v1/workflows/{workflow['id']}/publish", json={"note": "test"}, headers=account_headers)
            assert published.status_code == 200, published.text
            workflow["version"] = published.json()["version"]
        return workflow

    return _create
