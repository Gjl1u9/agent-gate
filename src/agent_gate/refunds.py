"""The rules an AI agent's refund request must pass.

Every function takes a psycopg connection in autocommit mode and opens its own transaction,
so the transaction boundaries are all in this file.
"""

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from psycopg import Connection
from psycopg.types.json import Jsonb

PENDING = "pending_approval"
EXECUTED = "executed"
REJECTED = "rejected"


@dataclass
class Outcome:
    status_code: int
    body: dict[str, Any]
    replayed: bool = False


def read_order(conn: Connection, session_id: str, order_id: int) -> Outcome:
    with conn.transaction():
        order = conn.execute(
            "SELECT id, customer, amount, refunded FROM orders WHERE id = %s", (order_id,)
        ).fetchone()
        if order is None:
            return Outcome(404, {"error": "order_not_found"})
        conn.execute(
            """INSERT INTO order_reads (session_id, order_id) VALUES (%s, %s)
               ON CONFLICT (session_id, order_id) DO UPDATE SET read_at = now()""",
            (session_id, order_id),
        )
    return Outcome(200, {**order, "refundable": order["amount"] - order["refunded"]})


def request_refund(
    conn: Connection,
    *,
    session_id: str,
    idempotency_key: str,
    order_id: int,
    amount: int,
    approval_threshold: int,
) -> Outcome:
    request_hash = _hash({"session_id": session_id, "order_id": order_id, "amount": amount})
    with conn.transaction():
        # Claim the key first. If another transaction holds the same key and has not committed,
        # this INSERT waits for it; then ON CONFLICT sees the committed row and we replay it.
        claimed = conn.execute(
            """INSERT INTO idempotency_keys (key, request_hash) VALUES (%s, %s)
               ON CONFLICT (key) DO NOTHING RETURNING key""",
            (idempotency_key, request_hash),
        ).fetchone()
        if claimed is None:
            return _replay(conn, idempotency_key, request_hash)

        outcome = _decide_refund(conn, session_id, order_id, amount, approval_threshold)
        # Rejections are stored too, so a retry gets the same answer instead of a second try.
        conn.execute(
            "UPDATE idempotency_keys SET status_code = %s, response = %s WHERE key = %s",
            (outcome.status_code, Jsonb(outcome.body), idempotency_key),
        )
    return outcome


def decide_pending_refund(
    conn: Connection, *, refund_id: int, approver: str, approve: bool
) -> Outcome:
    actor = f"approver:{approver}"
    with conn.transaction():
        # Lock order: refund row, then order row. request_refund only locks the order row,
        # so no two transactions can wait on each other in a cycle.
        refund = conn.execute(
            "SELECT id, order_id, amount, status FROM refunds WHERE id = %s FOR UPDATE",
            (refund_id,),
        ).fetchone()
        if refund is None:
            return Outcome(404, {"error": "refund_not_found"})
        if refund["status"] != PENDING:
            return Outcome(409, {"error": "already_decided", "refund": refund})

        if not approve:
            refund = _set_status(conn, refund_id, REJECTED, approver)
            _audit(conn, actor, "reject_refund", REJECTED, "rejected_by_approver", refund)
            return Outcome(200, refund)

        order = conn.execute(
            "SELECT amount, refunded FROM orders WHERE id = %s FOR UPDATE", (refund["order_id"],)
        ).fetchone()
        # Re-check at approval time: other refunds may have run since this one was requested.
        if refund["amount"] > order["amount"] - order["refunded"]:
            refund = _set_status(conn, refund_id, REJECTED, approver)
            _audit(conn, actor, "approve_refund", REJECTED, "exceeds_refundable", refund)
            return Outcome(409, {"error": "exceeds_refundable", "refund": refund})

        conn.execute(
            "UPDATE orders SET refunded = refunded + %s WHERE id = %s",
            (refund["amount"], refund["order_id"]),
        )
        refund = _set_status(conn, refund_id, EXECUTED, approver)
        _audit(conn, actor, "approve_refund", EXECUTED, None, refund)
    return Outcome(200, refund)


def get_refund(conn: Connection, refund_id: int) -> Outcome:
    refund = conn.execute(
        "SELECT id, order_id, amount, status, decided_by FROM refunds WHERE id = %s",
        (refund_id,),
    ).fetchone()
    if refund is None:
        return Outcome(404, {"error": "refund_not_found"})
    return Outcome(200, refund)


def list_audit(conn: Connection, order_id: int | None) -> Outcome:
    rows = conn.execute(
        """SELECT id, at, actor, action, decision, reason, order_id, refund_id FROM audit_log
           WHERE %(order_id)s::bigint IS NULL OR order_id = %(order_id)s
           ORDER BY id""",
        {"order_id": order_id},
    ).fetchall()
    return Outcome(200, {"entries": rows})


def _decide_refund(
    conn: Connection, session_id: str, order_id: int, amount: int, approval_threshold: int
) -> Outcome:
    actor = f"agent:{session_id}"
    # FOR UPDATE makes concurrent refunds on the same order run one after another, so each
    # one sees the refunded total left by the previous one.
    order = conn.execute(
        "SELECT amount, refunded FROM orders WHERE id = %s FOR UPDATE", (order_id,)
    ).fetchone()
    if order is None:
        return _reject(conn, actor, order_id, 404, "order_not_found")

    was_read = conn.execute(
        "SELECT 1 FROM order_reads WHERE session_id = %s AND order_id = %s",
        (session_id, order_id),
    ).fetchone()
    if was_read is None:
        return _reject(conn, actor, order_id, 403, "order_not_read")

    refundable = order["amount"] - order["refunded"]
    if amount > refundable:
        return _reject(conn, actor, order_id, 422, "exceeds_refundable", refundable=refundable)

    status = PENDING if amount > approval_threshold else EXECUTED
    refund = conn.execute(
        """INSERT INTO refunds (order_id, session_id, amount, status) VALUES (%s, %s, %s, %s)
           RETURNING id, order_id, amount, status""",
        (order_id, session_id, amount, status),
    ).fetchone()

    if status == PENDING:
        _audit(conn, actor, "request_refund", PENDING, "over_approval_threshold", refund)
        return Outcome(202, refund)

    conn.execute("UPDATE orders SET refunded = refunded + %s WHERE id = %s", (amount, order_id))
    _audit(conn, actor, "request_refund", EXECUTED, None, refund)
    return Outcome(201, refund)


def _replay(conn: Connection, key: str, request_hash: str) -> Outcome:
    stored = conn.execute(
        "SELECT request_hash, status_code, response FROM idempotency_keys WHERE key = %s", (key,)
    ).fetchone()
    if stored["request_hash"] != request_hash:
        # Same key, different request: almost certainly a client bug, so refuse loudly.
        return Outcome(409, {"error": "idempotency_key_reused"})
    return Outcome(stored["status_code"], stored["response"], replayed=True)


def _reject(
    conn: Connection, actor: str, order_id: int, status_code: int, reason: str, **extra: Any
) -> Outcome:
    _audit(conn, actor, "request_refund", REJECTED, reason, {"order_id": order_id, "id": None})
    return Outcome(status_code, {"error": reason, **extra})


def _set_status(conn: Connection, refund_id: int, status: str, approver: str) -> dict[str, Any]:
    return conn.execute(
        """UPDATE refunds SET status = %s, decided_by = %s, decided_at = now() WHERE id = %s
           RETURNING id, order_id, amount, status""",
        (status, approver, refund_id),
    ).fetchone()


def _audit(
    conn: Connection,
    actor: str,
    action: str,
    decision: str,
    reason: str | None,
    refund: dict[str, Any],
) -> None:
    conn.execute(
        """INSERT INTO audit_log (actor, action, decision, reason, order_id, refund_id)
           VALUES (%s, %s, %s, %s, %s, %s)""",
        (actor, action, decision, reason, refund["order_id"], refund["id"]),
    )


def _hash(request: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
