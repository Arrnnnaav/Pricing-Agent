CREATE TABLE IF NOT EXISTS catalog (
    sku TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    brand TEXT NOT NULL,
    category TEXT NOT NULL,
    our_price REAL NOT NULL CHECK (our_price > 0),
    cost REAL NOT NULL CHECK (cost > 0),
    stock INTEGER NOT NULL CHECK (stock >= 0)
);

CREATE TABLE IF NOT EXISTS competitor_prices (
    sku TEXT NOT NULL,
    competitor TEXT NOT NULL,
    price REAL NOT NULL CHECK (price > 0),
    in_stock INTEGER NOT NULL,
    date TEXT NOT NULL,
    PRIMARY KEY (sku, competitor, date),
    FOREIGN KEY (sku) REFERENCES catalog(sku)
);

-- Hot path: the Researcher pulls every SKU's history in one query.
CREATE INDEX IF NOT EXISTS idx_competitor_prices_sku_date ON competitor_prices (sku, date);

-- Raw scraped listings: competitor's own free-text product name, no SKU.
-- matched_sku is filled by the fuzzy matcher; true_sku is ground truth kept
-- only so the matcher's precision/recall can be measured.
CREATE TABLE IF NOT EXISTS competitor_listings (
    competitor    TEXT NOT NULL,
    listing_name  TEXT NOT NULL,
    price         REAL NOT NULL CHECK (price > 0),
    in_stock      INTEGER NOT NULL,
    date          TEXT NOT NULL,
    matched_sku   TEXT,
    match_score   REAL,
    true_sku      TEXT,
    PRIMARY KEY (competitor, listing_name, date)
);

-- Non-blocking human approval queue (APPROVAL_MODE=queue).
CREATE TABLE IF NOT EXISTS pending_approvals (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id              TEXT NOT NULL,
    sku                 TEXT NOT NULL,
    current_price       REAL NOT NULL,
    recommended_price   REAL NOT NULL,
    confidence          REAL NOT NULL,
    guardrail_violation TEXT NOT NULL,
    reasoning           TEXT NOT NULL,
    created_at          TEXT NOT NULL,
    status              TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'approved', 'rejected'))
);
