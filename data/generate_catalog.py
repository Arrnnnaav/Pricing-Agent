"""
Generates a synthetic consumer-electronics catalog for the pricing agent
to operate on. Run this once to produce data/catalog.csv:

    python data/generate_catalog.py

We don't use pure Faker output for product names (Faker doesn't know
what a laptop is) — instead we define a realistic brand/category
taxonomy ourselves and use Faker only for the pieces it's actually good
at (company-style names for a "house brand" feel, word variety for
model naming).
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import random
import numpy as np
import pandas as pd
from faker import Faker

import config

# Seed everything that generates randomness. Same seed -> same catalog,
# every time we run this script. That's what makes the demo reproducible
# and lets us write tests against known output later.
random.seed(config.RANDOM_SEED)
np.random.seed(config.RANDOM_SEED)
fake = Faker()
Faker.seed(config.RANDOM_SEED)

# Category -> (brands that plausibly sell it, typical cost range in USD)
# The cost range is what drives realistic pricing: a laptop and a phone
# case should not be drawn from the same distribution.
CATEGORIES = {
    "Laptops": {
        "brands": ["Dell", "HP", "Lenovo", "Asus", "Acer"],
        "cost_range": (400, 1400),
    },
    "Smartphones": {
        "brands": ["Samsung", "OnePlus", "Xiaomi", "Google", "Motorola"],
        "cost_range": (200, 900),
    },
    "Headphones": {
        "brands": ["Sony", "Bose", "JBL", "Sennheiser", "Anker"],
        "cost_range": (25, 250),
    },
    "Smartwatches": {
        "brands": ["Samsung", "Garmin", "Fitbit", "Amazfit", "Noise"],
        "cost_range": (40, 300),
    },
    "Tablets": {
        "brands": ["Samsung", "Lenovo", "Asus", "Xiaomi"],
        "cost_range": (120, 600),
    },
    "Monitors": {
        "brands": ["Dell", "LG", "Samsung", "Acer", "BenQ"],
        "cost_range": (100, 500),
    },
    "Accessories": {
        "brands": ["Anker", "Belkin", "Logitech", "boAt"],
        "cost_range": (8, 80),
    },
}


# Faker's default word() pulls generic English nouns ("Dog", "Election"),
# which reads as obviously fake for a product name. A curated tech-brand
# vocabulary is worth the manual list -- it's what actually sells the
# "this looks like a real catalog" illusion in a demo.
MODEL_WORDS = [
    "Vortex",
    "Pulse",
    "Nova",
    "Zenith",
    "Apex",
    "Flux",
    "Orion",
    "Nimbus",
    "Halo",
    "Ridge",
    "Volt",
    " Echo".strip(),
    "Prism",
    "Vertex",
    "Drift",
    "Surge",
    "Onyx",
    "Aria",
    "Comet",
    "Blaze",
    "Crest",
    "Fusion",
    "Spark",
]


def make_product_name(brand: str, category: str) -> str:
    """Builds a plausible model name like 'Dell Vortex Pro' by combining
    the brand with a tech-sounding model word + a version/tier suffix."""
    model_word = random.choice(MODEL_WORDS)
    suffix_pool = ["Pro", "Air", "Max", "Plus", "Lite", "SE", str(random.randint(2, 9))]
    suffix = random.choice(suffix_pool)
    return f"{brand} {model_word} {suffix}"


def make_sku_code(category: str, index: int) -> str:
    """Short, readable SKU code, e.g. 'LAP-0001'. Real catalogs use
    codes like this rather than raw product names as the primary key,
    since names can change (rebranding) but the SKU shouldn't."""
    prefix = category[:3].upper()
    return f"{prefix}-{index:04d}"


def price_from_cost(cost: float) -> float:
    """Turns a wholesale cost into a selling price using a randomized
    markup instead of a fixed multiplier, so margins vary realistically
    across the catalog (a $30 accessory and a $900 laptop don't carry
    the same margin % in a real business).

    We sample the markup from a normal distribution centered on a
    category-independent 35% margin target, then enforce our own
    MIN_MARGIN_PCT floor so we never generate a catalog item that
    already violates the guardrail we're about to build in
    guardrails.py -- that would make every downstream check trivially
    trip on data we made up ourselves.
    """
    target_margin = np.random.normal(loc=0.35, scale=0.08)
    target_margin = max(target_margin, config.MIN_MARGIN_PCT + 0.02)
    price = cost / (1 - target_margin)
    return round(price, 2)


def generate_catalog(num_skus: int = config.NUM_SKUS) -> pd.DataFrame:
    """Builds the full synthetic catalog as a DataFrame, one row per SKU.

    We spread SKUs roughly evenly across categories rather than picking
    category uniformly at random per item -- that keeps every category
    reasonably represented instead of risking, say, zero Tablets in a
    50-item catalog purely by chance.
    """
    category_names = list(CATEGORIES.keys())
    rows = []

    for i in range(1, num_skus + 1):
        category = category_names[(i - 1) % len(category_names)]
        info = CATEGORIES[category]
        brand = random.choice(info["brands"])

        cost = round(random.uniform(*info["cost_range"]), 2)
        our_price = price_from_cost(cost)

        # Stock levels: mostly comfortable, occasionally low. A Gaussian
        # here (clipped at 0) gives us a realistic mix rather than every
        # SKU having implausibly identical stock.
        stock = int(max(0, np.random.normal(loc=60, scale=30)))

        rows.append(
            {
                "sku": make_sku_code(category, i),
                "name": make_product_name(brand, category),
                "brand": brand,
                "category": category,
                "our_price": our_price,
                "cost": cost,
                "stock": stock,
            }
        )

    return pd.DataFrame(rows)


if __name__ == "__main__":
    from db.connection import get_connection, init_db
    from db.catalog_repo import insert_catalog_rows

    df = generate_catalog()
    os.makedirs(config.DATA_DIR, exist_ok=True)
    conn = get_connection(config.DB_PATH)
    init_db(conn)
    insert_catalog_rows(conn, df.to_dict(orient="records"))

    print(f"Generated {len(df)} SKUs -> {config.DB_PATH}")
    print(f"Categories: {df['category'].value_counts().to_dict()}")
    print(
        f"Avg margin: {((df['our_price'] - df['cost']) / df['our_price']).mean():.1%}"
    )
    print(
        f"Low-stock SKUs (<= {config.LOW_STOCK_THRESHOLD_UNITS}): "
        f"{(df['stock'] <= config.LOW_STOCK_THRESHOLD_UNITS).sum()}"
    )
    conn.close()
