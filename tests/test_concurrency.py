"""Many requests at the same instant, each on its own database connection.

These call the refund rules directly with one connection per thread, the same way the server
does with one connection per request, and use a barrier so all threads start together.
"""

import threading
from collections import Counter

from agent_gate import refunds

THREADS = 10


def run_at_once(pool, work):
    barrier = threading.Barrier(THREADS)
    outcomes = [None] * THREADS

    def worker(i):
        with pool.connection() as conn:
            barrier.wait()
            outcomes[i] = work(conn, i)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return outcomes


def order_state(pool, order_id):
    with pool.connection() as conn:
        refunded = conn.execute(
            "SELECT refunded FROM orders WHERE id = %s", (order_id,)
        ).fetchone()["refunded"]
        executed = conn.execute(
            "SELECT count(*) AS n FROM refunds WHERE order_id = %s AND status = 'executed'",
            (order_id,),
        ).fetchone()["n"]
    return refunded, executed


def test_parallel_refunds_never_exceed_the_order_amount(pool):
    # Order 1002 is 1200. Ten different requests of 300 each: exactly four can fit.
    with pool.connection() as conn:
        refunds.read_order(conn, "s1", 1002)

    outcomes = run_at_once(
        pool,
        lambda conn, i: refunds.request_refund(
            conn,
            session_id="s1",
            idempotency_key=f"key-{i}",
            order_id=1002,
            amount=300,
            approval_threshold=3000,
        ),
    )

    assert Counter(o.status_code for o in outcomes) == {201: 4, 422: 6}
    assert order_state(pool, 1002) == (1200, 4)


def test_parallel_retries_with_one_key_refund_once(pool):
    # The agent times out and fires the same request ten times at once.
    with pool.connection() as conn:
        refunds.read_order(conn, "s1", 1002)

    outcomes = run_at_once(
        pool,
        lambda conn, i: refunds.request_refund(
            conn,
            session_id="s1",
            idempotency_key="same-key",
            order_id=1002,
            amount=300,
            approval_threshold=3000,
        ),
    )

    assert {o.status_code for o in outcomes} == {201}
    assert len({o.body["id"] for o in outcomes}) == 1
    assert sum(not o.replayed for o in outcomes) == 1
    assert order_state(pool, 1002) == (300, 1)


def test_parallel_approvals_execute_once(pool):
    with pool.connection() as conn:
        refunds.read_order(conn, "s1", 1001)
        refund_id = refunds.request_refund(
            conn,
            session_id="s1",
            idempotency_key="big",
            order_id=1001,
            amount=4000,
            approval_threshold=3000,
        ).body["id"]

    outcomes = run_at_once(
        pool,
        lambda conn, i: refunds.decide_pending_refund(
            conn, refund_id=refund_id, approver=f"approver-{i}", approve=True
        ),
    )

    assert Counter(o.status_code for o in outcomes) == {200: 1, 409: THREADS - 1}
    assert order_state(pool, 1001) == (4000, 1)
