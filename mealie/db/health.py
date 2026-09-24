import asyncio
from concurrent.futures import Future
from threading import Lock, Thread
from time import monotonic

from sqlalchemy import text
from sqlalchemy.engine import Engine


class DatabaseProbe:
    """Share one in-flight check, even if a database driver stops responding.

    Request timeouts cannot cancel synchronous database I/O. A dedicated daemon
    thread bounds resource use to one check per probe, without consuming FastAPI's
    worker pool or preventing process exit when the startup deadline expires.
    """

    def __init__(self, engine: Engine, query: str = "SELECT 1") -> None:
        self._engine = engine
        self._query = text(query)
        self._lock = Lock()
        self._pending: Future[None] | None = None

    def start(self) -> Future[None]:
        with self._lock:
            if self._pending is None or self._pending.done():
                self._pending = Future()
                Thread(target=self._run, args=(self._pending,), name="database-probe", daemon=True).start()
            return self._pending

    def _run(self, result: Future[None]) -> None:
        try:
            # Use the application engine so pool exhaustion also fails readiness.
            # A fresh connection context rolls back and releases each check.
            with self._engine.connect() as connection:
                connection.execute(self._query).close()
        except Exception as exc:
            result.set_exception(exc)
        else:
            result.set_result(None)

    async def check(self, timeout: float) -> None:
        result = self.start()
        deadline = monotonic() + timeout
        while not result.done():
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("Database probe timed out")
            # Do not attach a new Future callback on every request: if the driver
            # hangs indefinitely those callbacks would accumulate on _pending.
            await asyncio.sleep(min(0.05, remaining))
        result.result()
