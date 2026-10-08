-- Portfolio Tracker schema (PostgreSQL 14+ / Supabase)
BEGIN;

CREATE TYPE asset_class AS ENUM ('equity', 'crypto', 'digital_asset', 'cash', 'bond');
CREATE TYPE tx_type AS ENUM ('BUY', 'SELL', 'TRANSFER', 'STAKING_REWARD', 'DIVIDEND', 'FEE');

CREATE TABLE assets (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    symbol        TEXT        NOT NULL,
    name          TEXT        NOT NULL,
    asset_class   asset_class NOT NULL,
    api_id        TEXT,                       -- e.g. CoinGecko id ('bitcoin') or Yahoo ticker ('SPY')
    base_currency CHAR(3)     NOT NULL DEFAULT 'USD',
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (symbol, asset_class)
);

CREATE TABLE transactions (
    id             BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id        UUID          NOT NULL,    -- Supabase: REFERENCES auth.users(id) ON DELETE CASCADE
    asset_id       BIGINT        NOT NULL REFERENCES assets(id),
    type           tx_type       NOT NULL,
    amount         NUMERIC(38,18) NOT NULL DEFAULT 0 CHECK (amount >= 0),   -- units
    price_per_unit NUMERIC(38,18) NOT NULL DEFAULT 0 CHECK (price_per_unit >= 0), -- in `currency`
    fee            NUMERIC(38,18) NOT NULL DEFAULT 0 CHECK (fee >= 0),      -- in `currency`
    currency       CHAR(3)       NOT NULL DEFAULT 'USD',
    "timestamp"    TIMESTAMPTZ   NOT NULL,
    notes          TEXT,
    created_at     TIMESTAMPTZ   NOT NULL DEFAULT now(),
    CONSTRAINT chk_amount_positive CHECK (type IN ('FEE') OR amount > 0)
);
CREATE INDEX idx_tx_user_time  ON transactions (user_id, "timestamp");
CREATE INDEX idx_tx_user_asset ON transactions (user_id, asset_id, "timestamp");

CREATE TABLE daily_snapshots (
    id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id         UUID           NOT NULL,
    asset_id        BIGINT         NOT NULL REFERENCES assets(id),
    date            DATE           NOT NULL,
    closing_price   NUMERIC(38,18) NOT NULL,   -- USD
    total_quantity  NUMERIC(38,18) NOT NULL,
    total_value_usd NUMERIC(38,8)  NOT NULL,
    UNIQUE (user_id, asset_id, date)
);
CREATE INDEX idx_snap_user_date ON daily_snapshots (user_id, date);

-- Needed for target allocation / rebalancing drift
CREATE TABLE target_allocations (
    user_id     UUID        NOT NULL,
    asset_class asset_class NOT NULL,
    target_pct  NUMERIC(5,2) NOT NULL CHECK (target_pct BETWEEN 0 AND 100),
    PRIMARY KEY (user_id, asset_class)
);

-- Supabase row-level security
ALTER TABLE transactions       ENABLE ROW LEVEL SECURITY;
ALTER TABLE daily_snapshots    ENABLE ROW LEVEL SECURITY;
ALTER TABLE target_allocations ENABLE ROW LEVEL SECURITY;
CREATE POLICY own_rows ON transactions       USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());
CREATE POLICY own_rows ON daily_snapshots    USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());
CREATE POLICY own_rows ON target_allocations USING (user_id = auth.uid()) WITH CHECK (user_id = auth.uid());

COMMIT;
