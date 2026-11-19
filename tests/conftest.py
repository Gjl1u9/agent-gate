"""Tests run against a real PostgreSQL (DATABASE_URL, default: the one in compose.yaml).

Locking and constraint behaviour is the point of this project, so there is no fake database.
"""

import pytest
from fastapi.testclient import TestClient

from agent_gate.db import create_pool, init_schema
from agent_gate.main import app
from agent_gate.seed import seed_orders

APPROVER = {"X-Approver-Token": "dev-approver-token"}


@pytest.fixture(scope="session")
def pool():
    pool = create_pool(max_size=20)
    init_schema(pool)
    yield pool
    pool.close()


@pytest.fixture(autouse=True)
def clean_db(pool):
    with pool.connection() as conn:
        conn.execute(
            "TRUNCATE audit_log, idempotency_keys, refunds, order_reads, orders "
            "RESTART IDENTITY CASCADE"
        )
    seed_orders(pool)


@pytest.fixture(scope="session")
def client():
    with TestClient(app) as client:
        yield client


@pytest.fixture
def agent(client):
    return Agent(client, session_id="session-1")


class Agent:
    """Plays the AI agent: reads orders and asks for refunds over HTTP."""

    def __init__(self, client: TestClient, session_id: str):
        self.client = client
        self.headers = {"X-Agent-Session": session_id}

    def read(self, order_id: int):
        return self.client.get(f"/orders/{order_id}", headers=self.headers)

    def refund(self, order_id: int, amount: int, key: str):
        return self.client.post(
            "/refunds",
            json={"order_id": order_id, "amount": amount},
            headers={**self.headers, "Idempotency-Key": key},
        )
