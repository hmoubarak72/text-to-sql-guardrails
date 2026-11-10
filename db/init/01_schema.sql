-- Text-to-SQL demo database: e-commerce schema "shop" plus application metadata schema "app_meta".
-- Executed once by the PostgreSQL entrypoint (docker-entrypoint-initdb.d) as the bootstrap superuser.

CREATE SCHEMA IF NOT EXISTS shop;
CREATE SCHEMA IF NOT EXISTS app_meta;

CREATE TABLE shop.regions (
    region_id      INTEGER PRIMARY KEY,
    name           TEXT NOT NULL UNIQUE
);

CREATE TABLE shop.customers (
    customer_id    INTEGER PRIMARY KEY,
    first_name     TEXT NOT NULL,
    last_name      TEXT NOT NULL,
    email          TEXT NOT NULL UNIQUE,
    phone          TEXT,
    city           TEXT NOT NULL,
    country        TEXT NOT NULL,
    region_id      INTEGER NOT NULL REFERENCES shop.regions(region_id),
    segment        TEXT NOT NULL CHECK (segment IN ('consumer', 'corporate', 'small_business')),
    signup_date    DATE NOT NULL
);

CREATE TABLE shop.categories (
    category_id         INTEGER PRIMARY KEY,
    name                TEXT NOT NULL UNIQUE,
    parent_category_id  INTEGER REFERENCES shop.categories(category_id)
);

CREATE TABLE shop.products (
    product_id     INTEGER PRIMARY KEY,
    name           TEXT NOT NULL,
    category_id    INTEGER NOT NULL REFERENCES shop.categories(category_id),
    unit_price     NUMERIC(10, 2) NOT NULL CHECK (unit_price > 0),
    unit_cost      NUMERIC(10, 2) NOT NULL CHECK (unit_cost > 0),
    is_active      BOOLEAN NOT NULL,
    launched_on    DATE NOT NULL
);

CREATE TABLE shop.orders (
    order_id       INTEGER PRIMARY KEY,
    customer_id    INTEGER NOT NULL REFERENCES shop.customers(customer_id),
    order_date     DATE NOT NULL,
    status         TEXT NOT NULL CHECK (status IN ('pending', 'shipped', 'delivered', 'cancelled', 'returned')),
    channel        TEXT NOT NULL CHECK (channel IN ('web', 'mobile', 'store')),
    shipping_cost  NUMERIC(10, 2) NOT NULL CHECK (shipping_cost >= 0)
);

CREATE TABLE shop.order_items (
    order_item_id  INTEGER PRIMARY KEY,
    order_id       INTEGER NOT NULL REFERENCES shop.orders(order_id),
    product_id     INTEGER NOT NULL REFERENCES shop.products(product_id),
    quantity       INTEGER NOT NULL CHECK (quantity > 0),
    unit_price     NUMERIC(10, 2) NOT NULL CHECK (unit_price > 0),
    discount       NUMERIC(4, 2) NOT NULL DEFAULT 0 CHECK (discount >= 0 AND discount < 1)
);

CREATE TABLE shop.payments (
    payment_id     INTEGER PRIMARY KEY,
    order_id       INTEGER NOT NULL REFERENCES shop.orders(order_id),
    method         TEXT NOT NULL CHECK (method IN ('card', 'paypal', 'bank_transfer', 'gift_card')),
    amount         NUMERIC(12, 2) NOT NULL CHECK (amount >= 0),
    paid_at        DATE NOT NULL
);

CREATE TABLE shop.refunds (
    refund_id      INTEGER PRIMARY KEY,
    order_id       INTEGER NOT NULL UNIQUE REFERENCES shop.orders(order_id),
    amount         NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
    reason         TEXT NOT NULL CHECK (reason IN ('damaged', 'wrong_item', 'late_delivery', 'changed_mind')),
    refunded_at    DATE NOT NULL
);

CREATE TABLE shop.employees (
    employee_id    INTEGER PRIMARY KEY,
    full_name      TEXT NOT NULL,
    job_title      TEXT NOT NULL,
    region_id      INTEGER NOT NULL REFERENCES shop.regions(region_id),
    salary         NUMERIC(12, 2) NOT NULL,
    hired_on       DATE NOT NULL
);

CREATE INDEX idx_customers_region ON shop.customers(region_id);
CREATE INDEX idx_products_category ON shop.products(category_id);
CREATE INDEX idx_orders_customer ON shop.orders(customer_id);
CREATE INDEX idx_orders_date ON shop.orders(order_date);
CREATE INDEX idx_order_items_order ON shop.order_items(order_id);
CREATE INDEX idx_order_items_product ON shop.order_items(product_id);
CREATE INDEX idx_payments_order ON shop.payments(order_id);
CREATE INDEX idx_refunds_order ON shop.refunds(order_id);

COMMENT ON TABLE shop.orders IS 'One row per customer order. order_date is the date the order was placed.';
COMMENT ON TABLE shop.order_items IS 'Line items. Line revenue = quantity * unit_price * (1 - discount).';
COMMENT ON TABLE shop.refunds IS 'Refunds issued against orders; an order can have at most one refund.';

-- ---------------------------------------------------------------------------
-- Application metadata (query history, guardrail audit log, feedback).
-- Never readable by the runtime query role.
-- ---------------------------------------------------------------------------
CREATE TABLE app_meta.query_log (
    query_id           UUID PRIMARY KEY,
    session_id         TEXT NOT NULL,
    created_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    question           TEXT NOT NULL,
    source             TEXT NOT NULL,            -- 'generated' | 'user_edited'
    decision           TEXT NOT NULL,            -- answer | answer_with_warnings | clarify | unsupported | abstain | blocked | error
    sql_text           TEXT,
    confidence         DOUBLE PRECISION,
    row_count          INTEGER,
    truncated          BOOLEAN,
    duration_ms        DOUBLE PRECISION,
    total_latency_ms   DOUBLE PRECISION,
    provider           TEXT,
    warnings           JSONB NOT NULL DEFAULT '[]'::jsonb,
    response_summary   JSONB NOT NULL DEFAULT '{}'::jsonb,
    result_preview     JSONB                      -- capped preview only, configurable (may be disabled)
);
CREATE INDEX idx_query_log_session ON app_meta.query_log(session_id, created_at DESC);

CREATE TABLE app_meta.guardrail_events (
    event_id      BIGSERIAL PRIMARY KEY,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    query_id      UUID,
    session_id    TEXT,
    stage         TEXT NOT NULL,                  -- parse | policy | plan | execution
    rule          TEXT NOT NULL,
    reason        TEXT NOT NULL,
    sql_text      TEXT                            -- truncated, string literals redacted
);

CREATE TABLE app_meta.feedback (
    feedback_id   BIGSERIAL PRIMARY KEY,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    query_id      UUID NOT NULL REFERENCES app_meta.query_log(query_id),
    session_id    TEXT NOT NULL,
    verdict       TEXT NOT NULL CHECK (verdict IN ('correct', 'incorrect')),
    comment       TEXT,
    corrected_sql TEXT,
    review_status TEXT NOT NULL DEFAULT 'pending_review'
                  CHECK (review_status IN ('pending_review', 'accepted_as_eval_case', 'accepted_as_example', 'rejected'))
);
