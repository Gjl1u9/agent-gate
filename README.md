# agent-gate

An approval gateway that sits between an AI agent and a refund API. The agent can only act
through it, and every request is checked against rules enforced in code and in PostgreSQL,
not in the prompt.

## Why

An agent with a refund tool can go wrong in ways a prompt cannot reliably prevent:

- refund an order it never looked at, with an amount it made up
- refund the same order several times, past what the customer paid
- retry after a timeout and refund twice
- be talked into a large refund by the customer

## Rules

| Rule | How it is enforced |
|---|---|
| Read before refund | A refund needs a prior `GET /orders/{id}` from the same agent session (`order_reads` table). |
| No over-refund | The order row is locked with `SELECT ... FOR UPDATE`, so concurrent refunds run one at a time and each sees the latest total. A `CHECK (refunded <= amount)` constraint backs this up. |
| Large refunds need a human | Above the threshold (default 3000), the refund is stored as `pending_approval`. Only a caller with the approver token can approve it, and approval executes exactly the stored order and amount after re-checking what is left. |
| Retries are safe | Every refund request carries an `Idempotency-Key`. The key is inserted first in the same transaction; a concurrent retry waits on the primary key, then replays the stored response. Reusing a key for a different request returns 409. |
| Everything is audited | Every executed, pending or rejected decision is written to `audit_log` in the same transaction as the decision. |

## API

| Method | Path | Who | Result |
|---|---|---|---|
| GET | `/orders/{id}` | agent (`X-Agent-Session`) | Order and refundable amount; records the read |
| POST | `/refunds` | agent (`X-Agent-Session`, `Idempotency-Key`) | 201 executed, 202 pending approval, 403 not read, 404 no order, 422 over the limit |
| GET | `/refunds/{id}` | agent | Refund status |
| POST | `/refunds/{id}/approve` | approver (`X-Approver-Token`) | 200 executed, 409 already decided or no longer fits |
| POST | `/refunds/{id}/reject` | approver | 200 rejected |
| GET | `/audit?order_id=` | approver | Audit entries |

Interactive docs are at `/docs` once the server runs.

## Run locally

Requires Docker and [uv](https://docs.astral.sh/uv/).

```bash
docker compose up -d --build
docker compose exec app python -m agent_gate.seed   # demo orders 1001 (5000), 1002 (1200), 1003 (800)
```

```bash
curl -H 'X-Agent-Session: demo' localhost:8000/orders/1001
curl -X POST localhost:8000/refunds \
  -H 'X-Agent-Session: demo' -H 'Idempotency-Key: demo-1' \
  -H 'Content-Type: application/json' -d '{"order_id": 1001, "amount": 4000}'
curl -X POST localhost:8000/refunds/1/approve \
  -H 'X-Approver-Token: dev-approver-token' \
  -H 'Content-Type: application/json' -d '{"approver": "alice"}'
```

Configuration: `DATABASE_URL`, `APPROVAL_THRESHOLD`, `APPROVER_TOKEN` (change it outside local
development).

## Tests

Tests run against a real PostgreSQL, because locking and constraints are what is being tested.
Note that they truncate the tables of the database they point at.

```bash
docker compose up -d db
uv run pytest
```

`tests/test_concurrency.py` fires ten requests at the same instant on separate connections and
checks that parallel refunds never exceed the order, that ten retries with one key refund once,
and that ten parallel approvals execute once.

CI (GitHub Actions) runs lint, the tests against a PostgreSQL service container, and a Docker
build on every push.

## Layout

```
src/agent_gate/
  schema.sql   tables and constraints
  refunds.py   the rules; every transaction boundary is in this file
  main.py      FastAPI routes, auth headers, connection pool
  db.py        pool and schema setup
  seed.py      demo orders
tests/
```
