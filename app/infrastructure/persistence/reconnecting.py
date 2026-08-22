"""A psycopg connection that transparently reopens if it went stale underneath us.

Found live, against the deployed backend: a single long-lived psycopg connection to Neon
(serverless Postgres) works until the connection sits idle, at which point **Neon closes it**
and every subsequent query fails with "the connection is closed" -- ``/readyz`` 503s and
``/api/reviews`` 500s -- until the process restarts. Render keeps the process alive far longer
than Neon keeps an idle connection alive, so this is not a rare edge; it is the steady state of
a low-traffic demo.

The fix is not "hold one connection forever" but "make sure the connection is alive before you
use it, and reopen it if it is not." Every adapter already funnels through ``.cursor()``, so
checking liveness there fixes all of them at once, with no change to any query.

why a ``SELECT 1`` liveness ping rather than only checking ``.closed``: psycopg does not mark a
connection ``.closed`` when the *server* drops it -- it only finds out on the next I/O. Without
the ping, the first query after an idle-close still fails; with it, that failure is caught here
and a fresh connection is handed over instead. One tiny round trip per operation is nothing
against this app's volume (a handful of queries per review, one status poll every 15s) and the
LLM calls that dominate every request's latency.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import psycopg
from psycopg.rows import dict_row


class ReconnectingConnection:
    """Drop-in for the ``psycopg.Connection`` the adapters used to hold. Exposes exactly the
    surface they use -- ``cursor()`` and ``close()`` -- and nothing else, so no adapter query
    changes."""

    def __init__(
        self,
        dsn: str,
        *,
        configure: Callable[[psycopg.Connection[Any]], None] | None = None,
    ) -> None:
        self._dsn = dsn
        self._configure = configure
        self._conn = self._open()

    def _open(self) -> psycopg.Connection[Any]:
        conn = psycopg.connect(self._dsn, autocommit=True, row_factory=dict_row)
        if self._configure is not None:
            self._configure(conn)
        return conn

    def _ensure_live(self) -> None:
        try:
            if self._conn.closed:
                raise psycopg.OperationalError("connection was closed")
            with self._conn.cursor() as cur:
                cur.execute("SELECT 1")
        except psycopg.OperationalError:
            self._conn = self._open()

    def cursor(self) -> psycopg.Cursor[Any]:
        self._ensure_live()
        return self._conn.cursor()

    def close(self) -> None:
        self._conn.close()
