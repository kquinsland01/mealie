import asyncio
from concurrent.futures import Future
from threading import Event
from unittest.mock import MagicMock, Mock

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError, ProgrammingError

from mealie.core.config import get_app_settings
from mealie.db import init_db
from mealie.db.health import DatabaseProbe


@pytest.mark.asyncio
async def test_stalled_probe_is_shared_and_recovers():
    entered, release = Event(), Event()
    engine = Mock()

    def connect():
        entered.set()
        assert release.wait(5)
        return MagicMock()

    engine.connect.side_effect = connect
    probe = DatabaseProbe(engine)
    pending = probe.start()
    try:
        assert entered.wait(1)
        results = await asyncio.gather(*(probe.check(0.02) for _ in range(20)), return_exceptions=True)
        assert all(isinstance(result, TimeoutError) for result in results)
        assert engine.connect.call_count == 1
        assert probe.start() is pending
    finally:
        release.set()
        pending.result(timeout=2)
    await probe.check(1)
    assert engine.connect.call_count == 2


@pytest.mark.asyncio
async def test_cancelled_request_does_not_cancel_shared_probe():
    release = Event()
    engine = Mock()
    engine.connect.side_effect = lambda: (release.wait(5), MagicMock())[1]
    probe = DatabaseProbe(engine)
    pending = probe.start()
    request = asyncio.create_task(probe.check(1))
    await asyncio.sleep(0)
    request.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await request
        assert not pending.cancelled()
    finally:
        release.set()
        pending.result(timeout=2)


@pytest.mark.asyncio
async def test_sqlite_connection_is_released_after_failure(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'health.db'}", pool_size=1, max_overflow=0)
    probe = DatabaseProbe(engine, "SELECT 1 FROM users LIMIT 1")
    try:
        with pytest.raises(OperationalError):
            await probe.check(1)
        with engine.begin() as conn:
            conn.execute(text("CREATE TABLE users (id INTEGER PRIMARY KEY)"))
        await probe.check(1)
        assert engine.pool.checkedout() == 0
    finally:
        engine.dispose()


def install_startup_probe(monkeypatch, outcomes):
    probe = Mock()

    def start():
        future = Future()
        outcome = next(outcomes)
        if outcome is None:
            future.set_result(None)
        else:
            future.set_exception(outcome)
        return future

    probe.start.side_effect = start
    monkeypatch.setattr(init_db, "DatabaseProbe", lambda _: probe)
    return probe


def test_startup_recovers_after_connection_failures(monkeypatch):
    settings = get_app_settings()
    monkeypatch.setattr(settings, "DB_STARTUP_TIMEOUT_SECONDS", 5.0)
    monkeypatch.setattr(settings, "DB_STARTUP_RETRY_INTERVAL_SECONDS", 0.5)
    now = [0.0]
    monkeypatch.setattr(init_db, "monotonic", lambda: now[0])
    monkeypatch.setattr(init_db, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    error = OperationalError("SELECT 1", {}, Exception("unavailable"))
    probe = install_startup_probe(monkeypatch, iter([error, error, None]))
    init_db.wait_for_database()
    assert probe.start.call_count == 3
    assert now[0] == 1.0


def test_startup_deadline_caps_retry_sleep(monkeypatch):
    settings = get_app_settings()
    monkeypatch.setattr(settings, "DB_STARTUP_TIMEOUT_SECONDS", 0.5)
    monkeypatch.setattr(settings, "DB_STARTUP_RETRY_INTERVAL_SECONDS", 1.0)
    now = [0.0]
    monkeypatch.setattr(init_db, "monotonic", lambda: now[0])
    monkeypatch.setattr(init_db, "sleep", lambda delay: now.__setitem__(0, now[0] + delay))
    error = OperationalError("SELECT 1", {}, Exception("unavailable"))
    probe = install_startup_probe(monkeypatch, iter([error]))
    with pytest.raises(ConnectionError, match="startup timeout"):
        init_db.wait_for_database()
    assert probe.start.call_count == 1
    assert now[0] == 0.5


def test_stalled_startup_obeys_deadline(monkeypatch):
    monkeypatch.setattr(get_app_settings(), "DB_STARTUP_TIMEOUT_SECONDS", 0.02)
    pending = Future()
    probe = Mock()
    probe.start.return_value = pending
    monkeypatch.setattr(init_db, "DatabaseProbe", lambda _: probe)
    with pytest.raises(ConnectionError, match="startup timeout"):
        init_db.wait_for_database()
    assert not pending.cancelled()
    assert probe.start.call_count == 1


def test_startup_does_not_retry_programming_errors(monkeypatch):
    error = ProgrammingError("SELECT 1", {}, Exception("invalid query"))
    probe = install_startup_probe(monkeypatch, iter([error]))
    with pytest.raises(ProgrammingError):
        init_db.wait_for_database()
    assert probe.start.call_count == 1


def test_failed_migration_is_not_retried(monkeypatch):
    wait = Mock()
    migrate = Mock(side_effect=RuntimeError("migration failed"))
    monkeypatch.setattr(init_db, "wait_for_database", wait)
    monkeypatch.setattr(init_db, "db_is_at_head", lambda _: False)
    monkeypatch.setattr(init_db.command, "upgrade", migrate)
    with pytest.raises(RuntimeError, match="migration failed"):
        init_db.main()
    wait.assert_called_once()
    migrate.assert_called_once()
