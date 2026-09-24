import asyncio
from threading import Event
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text

from mealie.app import app
from mealie.db.health import DatabaseProbe
from mealie.routes.app import app_health
from tests.utils import api_routes


def test_health_endpoints_allow_anonymous_requests(api_client: TestClient):
    for path in (api_routes.app_health_live, api_routes.app_health_ready):
        response = api_client.get(path)
        assert response.status_code == 200
        assert response.content == b""
        assert response.headers["Cache-Control"] == "no-store"


def test_readiness_failure_and_recovery(api_client: TestClient, monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'health.db'}")
    monkeypatch.setattr(app_health, "database_probe", DatabaseProbe(engine, "SELECT 1 FROM users LIMIT 1"))
    try:
        response = api_client.get(api_routes.app_health_ready)
        assert response.status_code == 503
        assert response.content == b""
        assert api_client.get(api_routes.app_health_live).status_code == 200
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        assert api_client.get(api_routes.app_health_ready).status_code == 200
    finally:
        engine.dispose()


def test_pool_exhaustion_does_not_fail_liveness(api_client: TestClient, monkeypatch, tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'health.db'}", pool_size=1, max_overflow=0, pool_timeout=0.02)
    monkeypatch.setattr(app_health, "database_probe", DatabaseProbe(engine))
    try:
        with engine.connect():
            assert api_client.get(api_routes.app_health_ready).status_code == 503
            assert api_client.get(api_routes.app_health_live).status_code == 200
        assert api_client.get(api_routes.app_health_ready).status_code == 200
    finally:
        engine.dispose()


@pytest.mark.asyncio
async def test_liveness_responds_while_readiness_is_blocked(monkeypatch):
    entered, release = Event(), Event()
    engine = MagicMock()

    def connect():
        entered.set()
        assert release.wait(5)
        return MagicMock()

    engine.connect.side_effect = connect
    probe = DatabaseProbe(engine)
    monkeypatch.setattr(app_health, "database_probe", probe)
    monkeypatch.setattr(app_health, "READINESS_TIMEOUT_SECONDS", 0.1)
    pending = probe.start()
    try:
        assert entered.wait(1)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            ready = asyncio.create_task(client.get(api_routes.app_health_ready))
            live = await asyncio.wait_for(client.get(api_routes.app_health_live), timeout=1)
            assert live.status_code == 200
            assert (await asyncio.wait_for(ready, timeout=1)).status_code == 503
            assert engine.connect.call_count == 1
    finally:
        release.set()
        pending.result(timeout=2)
