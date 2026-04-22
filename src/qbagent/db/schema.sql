-- qbagent portable schema.
-- Must parse and execute on DuckDB, SQLite, and PostgreSQL.
-- Portability rules:
--   * Booleans stored as INTEGER 0/1 (no BOOLEAN).
--   * Arrays stored as JSON-encoded TEXT.
--   * Monetary values stored as DECIMAL(18,4).
--   * Timestamps stored as TIMESTAMP; dates as DATE.
--   * No schemas, no backend-specific types or functions.

-- ----------------------------------------------------------------------------
-- accounts : QuickBooks chart of accounts
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS accounts (
    id                TEXT PRIMARY KEY,
    name              TEXT NOT NULL,
    account_type      TEXT,        -- Income, Expense, Bank, Asset, Liability, Equity
    account_sub_type  TEXT,
    parent_id         TEXT,
    active            INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    currency_code     TEXT,
    current_balance   DECIMAL(18, 4),
    created_at        TIMESTAMP,
    updated_at        TIMESTAMP,
    raw               TEXT          -- full QBO entity JSON
);
CREATE INDEX IF NOT EXISTS idx_accounts_type ON accounts (account_type);
CREATE INDEX IF NOT EXISTS idx_accounts_parent ON accounts (parent_id);

-- ----------------------------------------------------------------------------
-- invoices : header
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS invoices (
    id              TEXT PRIMARY KEY,
    doc_number      TEXT,
    customer_id     TEXT,
    customer_name   TEXT,
    invoice_date    DATE NOT NULL,
    due_date        DATE,
    total_amount    DECIMAL(18, 4) NOT NULL,
    balance         DECIMAL(18, 4) NOT NULL,
    status          TEXT NOT NULL,   -- Draft, Sent, Paid, PartiallyPaid, Voided
    currency_code   TEXT,
    memo            TEXT,
    created_at      TIMESTAMP,
    updated_at      TIMESTAMP,
    raw             TEXT
);
CREATE INDEX IF NOT EXISTS idx_invoices_date ON invoices (invoice_date);
CREATE INDEX IF NOT EXISTS idx_invoices_customer ON invoices (customer_id);
CREATE INDEX IF NOT EXISTS idx_invoices_status ON invoices (status);

-- ----------------------------------------------------------------------------
-- invoice_lines : one row per line item
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS invoice_lines (
    id            TEXT PRIMARY KEY,   -- invoice_id || ':' || line_num
    invoice_id    TEXT NOT NULL,
    line_num      INTEGER,
    description   TEXT,
    amount        DECIMAL(18, 4) NOT NULL,
    account_id    TEXT,
    item_id       TEXT,
    quantity      DECIMAL(18, 4),
    unit_price    DECIMAL(18, 4)
);
CREATE INDEX IF NOT EXISTS idx_invoice_lines_invoice ON invoice_lines (invoice_id);
CREATE INDEX IF NOT EXISTS idx_invoice_lines_account ON invoice_lines (account_id);

-- ----------------------------------------------------------------------------
-- bills : vendor bills (A/P)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bills (
    id              TEXT PRIMARY KEY,
    doc_number      TEXT,
    vendor_id       TEXT,
    vendor_name     TEXT,
    bill_date       DATE NOT NULL,
    due_date        DATE,
    total_amount    DECIMAL(18, 4) NOT NULL,
    balance         DECIMAL(18, 4) NOT NULL,
    status          TEXT NOT NULL,
    currency_code   TEXT,
    memo            TEXT,
    created_at      TIMESTAMP,
    updated_at      TIMESTAMP,
    raw             TEXT
);
CREATE INDEX IF NOT EXISTS idx_bills_date ON bills (bill_date);
CREATE INDEX IF NOT EXISTS idx_bills_vendor ON bills (vendor_id);
CREATE INDEX IF NOT EXISTS idx_bills_status ON bills (status);

-- ----------------------------------------------------------------------------
-- bill_lines
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS bill_lines (
    id           TEXT PRIMARY KEY,
    bill_id      TEXT NOT NULL,
    line_num     INTEGER,
    description  TEXT,
    amount       DECIMAL(18, 4) NOT NULL,
    account_id   TEXT
);
CREATE INDEX IF NOT EXISTS idx_bill_lines_bill ON bill_lines (bill_id);
CREATE INDEX IF NOT EXISTS idx_bill_lines_account ON bill_lines (account_id);

-- ----------------------------------------------------------------------------
-- expenses : QBO Purchase entity (direct cash purchases)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS expenses (
    id             TEXT PRIMARY KEY,
    payment_type   TEXT,
    account_id     TEXT,
    entity_id      TEXT,
    entity_name    TEXT,
    expense_date   DATE NOT NULL,
    total_amount   DECIMAL(18, 4) NOT NULL,
    status         TEXT NOT NULL,
    currency_code  TEXT,
    memo           TEXT,
    created_at     TIMESTAMP,
    updated_at     TIMESTAMP,
    raw            TEXT
);
CREATE INDEX IF NOT EXISTS idx_expenses_date ON expenses (expense_date);
CREATE INDEX IF NOT EXISTS idx_expenses_account ON expenses (account_id);
CREATE INDEX IF NOT EXISTS idx_expenses_status ON expenses (status);

-- ----------------------------------------------------------------------------
-- transactions : general ledger view (QBO TransactionList)
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS transactions (
    id                TEXT PRIMARY KEY,
    transaction_date  DATE NOT NULL,
    transaction_type  TEXT NOT NULL,
    doc_number        TEXT,
    account_id        TEXT,
    debit             DECIMAL(18, 4),
    credit            DECIMAL(18, 4),
    amount            DECIMAL(18, 4),
    entity_id         TEXT,
    entity_name       TEXT,
    memo              TEXT,
    created_at        TIMESTAMP,
    updated_at        TIMESTAMP,
    raw               TEXT
);
CREATE INDEX IF NOT EXISTS idx_transactions_date ON transactions (transaction_date);
CREATE INDEX IF NOT EXISTS idx_transactions_account ON transactions (account_id);
CREATE INDEX IF NOT EXISTS idx_transactions_type ON transactions (transaction_type);

-- ----------------------------------------------------------------------------
-- sync_log : per-entity sync cursor and status
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS sync_log (
    entity              TEXT PRIMARY KEY,
    last_cursor         TEXT,
    last_sync_at        TIMESTAMP NOT NULL,
    last_sync_status    TEXT NOT NULL,   -- success, error, in_progress
    last_error          TEXT,
    rows_synced         INTEGER NOT NULL DEFAULT 0
);

-- ----------------------------------------------------------------------------
-- memory_rules : curated + user-approved rules fed to the agent
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS memory_rules (
    id          TEXT PRIMARY KEY,
    text        TEXT NOT NULL,
    source      TEXT NOT NULL,  -- curated, user
    tags        TEXT,           -- JSON array of strings
    created_at  TIMESTAMP NOT NULL,
    active      INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);
CREATE INDEX IF NOT EXISTS idx_memory_rules_source ON memory_rules (source);

-- ----------------------------------------------------------------------------
-- query_log : every question asked, its SQL, result, and audit trail
-- ----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS query_log (
    id                    TEXT PRIMARY KEY,
    session_id            TEXT,
    created_at            TIMESTAMP NOT NULL,
    question              TEXT NOT NULL,
    generated_sql         TEXT,
    validation_errors     TEXT,    -- JSON
    validation_warnings   TEXT,    -- JSON
    executed_sql          TEXT,
    row_count             INTEGER,
    answer                TEXT,
    sanity_warnings       TEXT,    -- JSON
    correction_detected   INTEGER NOT NULL DEFAULT 0 CHECK (correction_detected IN (0, 1)),
    latency_ms            INTEGER,
    llm_model             TEXT
);
CREATE INDEX IF NOT EXISTS idx_query_log_session ON query_log (session_id);
CREATE INDEX IF NOT EXISTS idx_query_log_created ON query_log (created_at);
