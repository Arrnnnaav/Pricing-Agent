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
