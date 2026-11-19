-- Orders the agent can refund. The CHECK on refunded is the last line of defence:
-- even if the application logic had a race, the database refuses to refund more than was paid.
CREATE TABLE IF NOT EXISTS orders (
    id        bigint  PRIMARY KEY,
    customer  text    NOT NULL,
    amount    integer NOT NULL CHECK (amount > 0),
    refunded  integer NOT NULL DEFAULT 0,
    CHECK (refunded >= 0 AND refunded <= amount)
);

-- Which agent session has looked at which order. A refund needs a prior read.
CREATE TABLE IF NOT EXISTS order_reads (
    session_id  text        NOT NULL,
    order_id    bigint      NOT NULL REFERENCES orders (id),
    read_at     timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (session_id, order_id)
);

CREATE TABLE IF NOT EXISTS refunds (
    id          bigserial   PRIMARY KEY,
    order_id    bigint      NOT NULL REFERENCES orders (id),
    session_id  text        NOT NULL,
    amount      integer     NOT NULL CHECK (amount > 0),
    status      text        NOT NULL
                CHECK (status IN ('pending_approval', 'executed', 'rejected')),
    decided_by  text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    decided_at  timestamptz
);

-- One row per Idempotency-Key. The primary key makes a retried request with the same key
-- wait for the first one to commit, then replay its stored response instead of running again.
CREATE TABLE IF NOT EXISTS idempotency_keys (
    key           text        PRIMARY KEY,
    request_hash  text        NOT NULL,
    status_code   integer,
    response      jsonb,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS audit_log (
    id         bigserial   PRIMARY KEY,
    at         timestamptz NOT NULL DEFAULT now(),
    actor      text        NOT NULL,
    action     text        NOT NULL,
    decision   text        NOT NULL,
    reason     text,
    order_id   bigint,
    refund_id  bigint
);

CREATE INDEX IF NOT EXISTS audit_log_order_id_idx ON audit_log (order_id);
