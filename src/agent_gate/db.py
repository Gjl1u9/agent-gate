import os
from importlib.resources import files

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DEFAULT_DATABASE_URL = "postgresql://agentgate:agentgate@localhost:5432/agentgate"

# Any constant works; it only has to be the same in every instance that runs migrations.
_SCHEMA_LOCK_ID = 7_301_001


def create_pool(url: str | None = None, max_size: int = 10) -> ConnectionPool:
    # autocommit=True: nothing is wrapped in a transaction unless the code asks for one
    # with conn.transaction(), so every transaction boundary is visible in refunds.py.
    return ConnectionPool(
        url or os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL),
        min_size=1,
        max_size=max_size,
        kwargs={"autocommit": True, "row_factory": dict_row},
        open=True,
    )


def init_schema(pool: ConnectionPool) -> None:
    sql = files("agent_gate").joinpath("schema.sql").read_text()
    with pool.connection() as conn, conn.transaction():
        # Several instances may start at once (e.g. on Cloud Run); let one create the tables.
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (_SCHEMA_LOCK_ID,))
        conn.execute(sql)
