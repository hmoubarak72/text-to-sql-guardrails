#!/usr/bin/env bash
# Creates least-privilege roles. Runs once inside the PostgreSQL entrypoint as the bootstrap superuser.
#   t2s_app    - application role: read/write ONLY app_meta (history, audit, feedback). No access to shop data.
#   t2s_reader - runtime query role: column-level SELECT on shop, read-only by default, statement timeout.
# Initialization / migration privileges stay with the bootstrap superuser (POSTGRES_USER), which the API never uses.
set -euo pipefail

: "${T2S_APP_PASSWORD:?T2S_APP_PASSWORD must be set}"
: "${T2S_READER_PASSWORD:?T2S_READER_PASSWORD must be set}"
DB="${POSTGRES_DB:-t2s}"
TIMEOUT_MS="${T2S_READER_STATEMENT_TIMEOUT_MS:-5000}"

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$DB" \
  -v app_pw="$T2S_APP_PASSWORD" -v reader_pw="$T2S_READER_PASSWORD" -v db="$DB" -v timeout_ms="$TIMEOUT_MS" <<'EOSQL'
CREATE ROLE t2s_app LOGIN PASSWORD :'app_pw' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT;
CREATE ROLE t2s_reader LOGIN PASSWORD :'reader_pw' NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT CONNECTION LIMIT 20;

REVOKE ALL ON DATABASE :"db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"db" TO t2s_app, t2s_reader;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON SCHEMA shop, app_meta FROM PUBLIC;

-- Application role: app_meta only.
GRANT USAGE ON SCHEMA app_meta TO t2s_app;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA app_meta TO t2s_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA app_meta TO t2s_app;

-- Runtime reader: SELECT on shop, with column-level grants that exclude sensitive columns
-- (customers.email, customers.phone, employees.salary). This is a second line of defense
-- behind the application policy engine.
GRANT USAGE ON SCHEMA shop TO t2s_reader;
GRANT SELECT ON shop.regions, shop.categories, shop.products, shop.orders,
                shop.order_items, shop.payments, shop.refunds TO t2s_reader;
GRANT SELECT (customer_id, first_name, last_name, city, country, region_id, segment, signup_date)
      ON shop.customers TO t2s_reader;
GRANT SELECT (employee_id, full_name, job_title, region_id, hired_on)
      ON shop.employees TO t2s_reader;

ALTER ROLE t2s_reader SET default_transaction_read_only = on;
ALTER ROLE t2s_reader SET statement_timeout = :'timeout_ms';
ALTER ROLE t2s_reader SET idle_in_transaction_session_timeout = '10s';
ALTER ROLE t2s_reader SET search_path = shop;
ALTER ROLE t2s_reader SET work_mem = '16MB';
ALTER ROLE t2s_reader SET temp_file_limit = '64MB';

-- Planner statistics so EXPLAIN estimates are meaningful immediately after seeding.
ANALYZE;
EOSQL
echo "t2s roles created"
