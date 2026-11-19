from conftest import APPROVER, Agent

# Demo orders from agent_gate.seed: 1001 = 5000, 1002 = 1200, 1003 = 800. Threshold = 3000.


def refunded(client, order_id: int) -> int:
    return client.get(f"/orders/{order_id}", headers={"X-Agent-Session": "checker"}).json()[
        "refunded"
    ]


def test_small_refund_after_read_executes(agent):
    assert agent.read(1002).json()["refundable"] == 1200

    res = agent.refund(1002, 500, key="k1")

    assert res.status_code == 201
    assert res.json()["status"] == "executed"
    assert refunded(agent.client, 1002) == 500


def test_refund_without_read_is_rejected(agent):
    res = agent.refund(1002, 500, key="k1")

    assert res.status_code == 403
    assert res.json() == {"error": "order_not_read"}
    assert refunded(agent.client, 1002) == 0


def test_read_by_another_session_does_not_count(client, agent):
    Agent(client, session_id="session-2").read(1002)

    assert agent.refund(1002, 500, key="k1").status_code == 403


def test_refund_of_unknown_order_is_rejected(agent):
    assert agent.refund(9999, 100, key="k1").status_code == 404


def test_refund_over_remaining_amount_is_rejected(agent):
    agent.read(1002)
    agent.refund(1002, 1000, key="k1")

    res = agent.refund(1002, 300, key="k2")

    assert res.status_code == 422
    assert res.json() == {"error": "exceeds_refundable", "refundable": 200}


def test_non_positive_amount_is_rejected_by_validation(agent):
    agent.read(1002)
    assert agent.refund(1002, 0, key="k1").status_code == 422


def test_large_refund_waits_for_approval(client, agent):
    agent.read(1001)

    res = agent.refund(1001, 4000, key="k1")
    assert res.status_code == 202
    assert res.json()["status"] == "pending_approval"
    assert refunded(client, 1001) == 0

    refund_id = res.json()["id"]
    approved = client.post(
        f"/refunds/{refund_id}/approve", json={"approver": "alice"}, headers=APPROVER
    )
    assert approved.status_code == 200
    assert approved.json()["status"] == "executed"
    assert refunded(client, 1001) == 4000


def test_approval_needs_the_approver_token(client, agent):
    agent.read(1001)
    refund_id = agent.refund(1001, 4000, key="k1").json()["id"]

    res = client.post(
        f"/refunds/{refund_id}/approve",
        json={"approver": "agent"},
        headers={"X-Approver-Token": "guess"},
    )

    assert res.status_code == 401
    assert refunded(client, 1001) == 0


def test_rejected_refund_is_not_executed(client, agent):
    agent.read(1001)
    refund_id = agent.refund(1001, 4000, key="k1").json()["id"]

    res = client.post(f"/refunds/{refund_id}/reject", json={"approver": "alice"}, headers=APPROVER)

    assert res.json()["status"] == "rejected"
    assert refunded(client, 1001) == 0


def test_a_refund_can_be_decided_only_once(client, agent):
    agent.read(1001)
    refund_id = agent.refund(1001, 4000, key="k1").json()["id"]
    client.post(f"/refunds/{refund_id}/approve", json={"approver": "alice"}, headers=APPROVER)

    again = client.post(f"/refunds/{refund_id}/approve", json={"approver": "bob"}, headers=APPROVER)

    assert again.status_code == 409
    assert refunded(client, 1001) == 4000


def test_approval_rechecks_the_remaining_amount(client, agent):
    # Two pending refunds that each fit, but not together.
    agent.read(1001)
    first = agent.refund(1001, 4000, key="k1").json()["id"]
    second = agent.refund(1001, 4000, key="k2").json()["id"]

    client.post(f"/refunds/{first}/approve", json={"approver": "alice"}, headers=APPROVER)
    res = client.post(f"/refunds/{second}/approve", json={"approver": "alice"}, headers=APPROVER)

    assert res.status_code == 409
    assert res.json()["refund"]["status"] == "rejected"
    assert refunded(client, 1001) == 4000


def test_retry_with_same_key_replays_without_refunding_again(client, agent):
    agent.read(1002)
    first = agent.refund(1002, 500, key="retry-me")

    second = agent.refund(1002, 500, key="retry-me")

    assert second.status_code == first.status_code == 201
    assert second.json() == first.json()
    assert second.headers["Idempotent-Replayed"] == "true"
    assert refunded(client, 1002) == 500


def test_rejection_is_replayed_too(agent):
    # The order was not read, so the first try is rejected. Reading it afterwards does not
    # turn a retry of the same request into a success: the key already has an answer.
    assert agent.refund(1002, 500, key="k1").status_code == 403
    agent.read(1002)

    assert agent.refund(1002, 500, key="k1").status_code == 403


def test_same_key_with_different_request_is_refused(client, agent):
    agent.read(1002)
    agent.refund(1002, 500, key="k1")

    res = agent.refund(1002, 600, key="k1")

    assert res.status_code == 409
    assert res.json() == {"error": "idempotency_key_reused"}
    assert refunded(client, 1002) == 500


def test_every_decision_is_audited(client, agent):
    agent.refund(1001, 100, key="k1")  # rejected: not read
    agent.read(1001)
    agent.refund(1001, 100, key="k2")  # executed
    pending = agent.refund(1001, 4000, key="k3").json()["id"]
    client.post(f"/refunds/{pending}/approve", json={"approver": "alice"}, headers=APPROVER)

    entries = client.get("/audit", params={"order_id": 1001}, headers=APPROVER).json()["entries"]

    assert [(e["actor"], e["decision"], e["reason"]) for e in entries] == [
        ("agent:session-1", "rejected", "order_not_read"),
        ("agent:session-1", "executed", None),
        ("agent:session-1", "pending_approval", "over_approval_threshold"),
        ("approver:alice", "executed", None),
    ]


def test_audit_log_needs_the_approver_token(client):
    assert client.get("/audit").status_code == 422
    assert client.get("/audit", headers={"X-Approver-Token": "guess"}).status_code == 401
