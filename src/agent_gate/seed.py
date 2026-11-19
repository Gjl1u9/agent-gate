"""Load demo orders: `uv run python -m agent_gate.seed`."""

from psycopg_pool import ConnectionPool

from agent_gate.db import create_pool, init_schema

DEMO_ORDERS = [
    (1001, "alice", 5000),
    (1002, "bob", 1200),
    (1003, "carol", 800),
]


def seed_orders(pool: ConnectionPool) -> None:
    with pool.connection() as conn:
        conn.cursor().executemany(
            """INSERT INTO orders (id, customer, amount) VALUES (%s, %s, %s)
               ON CONFLICT (id) DO NOTHING""",
            DEMO_ORDERS,
        )


if __name__ == "__main__":
    with create_pool() as pool:
        init_schema(pool)
        seed_orders(pool)
    print(f"Seeded {len(DEMO_ORDERS)} orders.")
