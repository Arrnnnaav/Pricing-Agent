"""
Generates synthetic competitor price history for every SKU in the catalog.

Models each competitor's daily price as a mean-reverting random walk
(an Ornstein-Uhlenbeck-style process) rather than independent random
draws per day. Real competitor prices move gradually and trend --
they don't teleport to a fresh random number every morning -- so this
gives the Analyst agent something meaningful to compute a trend from.

Run this once to produce data/competitor_price_history.csv:
    python data/mock_price_generator.py
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import random
from datetime import date, timedelta

import numpy as np
import pandas as pd
from faker import Faker

import config

random.seed(config.RANDOM_SEED)
np.random.seed(config.RANDOM_SEED)
fake = Faker()
Faker.seed(config.RANDOM_SEED)


def generate_competitor_names(n: int = config.NUM_COMPETITORS) -> list[str]:
    """Builds n plausible e-commerce competitor names, e.g. 'ByteMart',
    'CircuitHub'. We don't use real company names (TechCorp, Amazon,
    etc.) -- this is synthetic data and should look obviously
    illustrative, not imply we scraped a real retailer."""
    prefixes = [
        "Byte",
        "Circuit",
        "Volt",
        "Gadget",
        "Nexus",
        "Prime",
        "Quantum",
        "Spark",
    ]
    suffixes = ["Mart", "Hub", "Bazaar", "Depot", "Point", "Store", "Cart"]
    names = set()
    while len(names) < n:
        names.add(random.choice(prefixes) + random.choice(suffixes))
    return sorted(names)


# Each competitor gets a fixed "pricing personality" -- some competitors
# are systematically cheaper than us, some pricier, and each has its own
# day-to-day volatility. This is what makes competitor A behave
# differently from competitor B across the whole catalog, instead of
# every competitor being statistically identical noise.
COMPETITORS = generate_competitor_names()

COMPETITOR_PROFILES = {
    name: {
        # Multiplicative bias applied to our price to get this
        # competitor's "true" target price -- e.g. 0.94 means this
        # competitor tends to sit 6% below us.
        "price_bias": np.random.normal(loc=1.0, scale=0.06),
        # Daily volatility for this competitor's random walk step.
        "volatility": abs(
            np.random.normal(loc=config.DAILY_PRICE_STEP_STD_PCT, scale=0.005)
        ),
        # How strongly this competitor's price is pulled back toward
        # its target each day (0 = no pull / pure random walk,
        # 1 = snaps back immediately). Real markets sit somewhere in between.
        "mean_reversion": np.random.uniform(0.15, 0.35),
        # Small daily chance this competitor shows as out of stock.
        "stockout_prob": np.random.uniform(0.01, 0.06),
    }
    for name in COMPETITORS
}


def generate_walk(target_price: float, profile: dict, days: int) -> list[float]:
    """Generates `days` daily prices for one (SKU, competitor) pair using
    a mean-reverting random walk:

        price[t] = price[t-1] + reversion_pull + random_step

    where reversion_pull nudges the price back toward `target_price`
    (scaled by mean_reversion), and random_step is Gaussian noise scaled
    by this competitor's volatility. This is a simplified
    Ornstein-Uhlenbeck process: prices can drift day to day like a real
    market, but won't wander off indefinitely because the reversion term
    keeps pulling them home.
    """
    prices = [target_price]
    for _ in range(1, days):
        prev = prices[-1]
        reversion_pull = profile["mean_reversion"] * (target_price - prev)
        random_step = np.random.normal(loc=0, scale=prev * profile["volatility"])
        next_price = prev + reversion_pull + random_step
        next_price = max(next_price, target_price * 0.5)  # floor: never absurdly cheap
        prices.append(round(next_price, 2))
    return prices


def generate_price_history(
    catalog: pd.DataFrame, days: int = config.PRICE_HISTORY_DAYS
) -> pd.DataFrame:
    """Builds the full history: for every SKU, for every competitor,
    a `days`-long price walk ending "today". Returns one row per
    (sku, competitor, date) -- long format, which is what pandas and
    later our Pydantic models both prefer to work with over a wide
    date-as-columns layout.
    """
    today = date.today()
    date_range = [today - timedelta(days=days - 1 - i) for i in range(days)]

    rows = []
    for _, item in catalog.iterrows():
        for competitor in COMPETITORS:
            profile = COMPETITOR_PROFILES[competitor]
            target_price = round(item["our_price"] * profile["price_bias"], 2)
            walk = generate_walk(target_price, profile, days)

            for day_offset, price in enumerate(walk):
                in_stock = np.random.random() > profile["stockout_prob"]
                rows.append(
                    {
                        "sku": item["sku"],
                        "competitor": competitor,
                        "date": date_range[day_offset].isoformat(),
                        "price": price,
                        "in_stock": in_stock,
                    }
                )

    return pd.DataFrame(rows)


if __name__ == "__main__":
    from db.connection import get_connection, init_db
    from db.catalog_repo import get_catalog_df
    from db.competitor_repo import insert_competitor_price_rows

    conn = get_connection(config.DB_PATH)
    init_db(conn)
    catalog = get_catalog_df(conn)

    df = generate_price_history(catalog)
    insert_competitor_price_rows(conn, df.to_dict(orient="records"))

    print(f"Generated {len(df)} price points -> {config.DB_PATH}")
    print(
        f"  {len(catalog)} SKUs x {len(COMPETITORS)} competitors x "
        f"{config.PRICE_HISTORY_DAYS} days"
    )
    print(f"Competitors: {COMPETITORS}")
    for name, profile in COMPETITOR_PROFILES.items():
        print(
            f"  {name}: price_bias={profile['price_bias']:.2f}, "
            f"volatility={profile['volatility']:.3f}, "
            f"mean_reversion={profile['mean_reversion']:.2f}"
        )
    conn.close()
