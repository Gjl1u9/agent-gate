import os
import secrets
from collections.abc import Iterator
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse
from psycopg import Connection
from pydantic import BaseModel, Field

from agent_gate import refunds
from agent_gate.db import create_pool, init_schema

# Refunds above this amount wait for a human. Amounts are whole TWD.
APPROVAL_THRESHOLD = int(os.environ.get("APPROVAL_THRESHOLD", "3000"))
APPROVER_TOKEN = os.environ.get("APPROVER_TOKEN", "dev-approver-token")


@asynccontextmanager
async def lifespan(app: FastAPI):
    pool = create_pool()
    init_schema(pool)
    app.state.pool = pool
    yield
    pool.close()


app = FastAPI(title="agent-gate", lifespan=lifespan)


def get_conn(request: Request) -> Iterator[Connection]:
    with request.app.state.pool.connection() as conn:
        yield conn


def require_approver(x_approver_token: Annotated[str, Header()]) -> None:
    # The agent never gets this token, so it cannot approve its own refunds.
    if not secrets.compare_digest(x_approver_token, APPROVER_TOKEN):
        raise HTTPException(status_code=401, detail="invalid approver token")


Conn = Annotated[Connection, Depends(get_conn)]
AgentSession = Annotated[str, Header(alias="X-Agent-Session", min_length=1)]
IdempotencyKey = Annotated[str, Header(alias="Idempotency-Key", min_length=1, max_length=200)]
ApproverOnly = [Depends(require_approver)]


class RefundRequest(BaseModel):
    order_id: int
    amount: int = Field(gt=0)


class Decision(BaseModel):
    approver: str = Field(min_length=1)


def respond(outcome: refunds.Outcome) -> JSONResponse:
    headers = {"Idempotent-Replayed": "true"} if outcome.replayed else None
    return JSONResponse(
        jsonable_encoder(outcome.body), status_code=outcome.status_code, headers=headers
    )


# Handlers are plain `def`: psycopg is a blocking driver, and FastAPI runs `def` handlers in a
# thread pool, so a slow query does not block the event loop.


@app.get("/healthz")
def healthz(conn: Conn) -> dict[str, bool]:
    conn.execute("SELECT 1")
    return {"ok": True}


@app.get("/orders/{order_id}")
def get_order(order_id: int, session_id: AgentSession, conn: Conn) -> JSONResponse:
    return respond(refunds.read_order(conn, session_id, order_id))


@app.post("/refunds")
def create_refund(
    body: RefundRequest, session_id: AgentSession, idempotency_key: IdempotencyKey, conn: Conn
) -> JSONResponse:
    return respond(
        refunds.request_refund(
            conn,
            session_id=session_id,
            idempotency_key=idempotency_key,
            order_id=body.order_id,
            amount=body.amount,
            approval_threshold=APPROVAL_THRESHOLD,
        )
    )


@app.get("/refunds/{refund_id}")
def get_refund(refund_id: int, session_id: AgentSession, conn: Conn) -> JSONResponse:
    return respond(refunds.get_refund(conn, refund_id))


@app.post("/refunds/{refund_id}/approve", dependencies=ApproverOnly)
def approve_refund(refund_id: int, body: Decision, conn: Conn) -> JSONResponse:
    return respond(
        refunds.decide_pending_refund(
            conn, refund_id=refund_id, approver=body.approver, approve=True
        )
    )


@app.post("/refunds/{refund_id}/reject", dependencies=ApproverOnly)
def reject_refund(refund_id: int, body: Decision, conn: Conn) -> JSONResponse:
    return respond(
        refunds.decide_pending_refund(
            conn, refund_id=refund_id, approver=body.approver, approve=False
        )
    )


@app.get("/audit", dependencies=ApproverOnly)
def audit(conn: Conn, order_id: int | None = None) -> JSONResponse:
    return respond(refunds.list_audit(conn, order_id))
