"""Turns clean (sku, competitor, date, price) walks into what a scraper
actually sees: each competitor's own free-text listing name, no SKU code.

Each competitor has a fixed naming style (the same product keeps the same
listing name across days, as on a real site), built from realistic noise:
case changes, token reordering, marketing words, a dropped tier word, a
brand written differently, and occasional typos. A share of listings are
decoys -- products we don't sell -- which a good matcher must leave
unmatched rather than force onto the nearest SKU.
"""

import random

import pandas as pd

import config

MARKETING = ["(2026 Edition)", "- Official", "with 1Y Warranty", "| Free Shipping",
             "New", "Renewed", "Black", "Silver", "Bundle"]  # fmt: skip
TIER_WORDS = {"Pro", "Air", "Max", "Plus", "Lite", "SE"}
DECOY_WORDS = ["Zephyr", "Quasar", "Titan", "Polaris", "Kestrel", "Mirage"]


def _typo(word: str, rng: random.Random) -> str:
    if len(word) < 5:
        return word
    i = rng.randrange(1, len(word) - 2)
    return word[:i] + word[i + 1] + word[i] + word[i + 2 :]


def noisy_name(name: str, rng: random.Random) -> str:
    tokens = name.split()
    brand, rest = tokens[0], tokens[1:]
    if rng.random() < 0.3:
        rest = [t for t in rest if t not in TIER_WORDS] or rest
    if rng.random() < 0.4:
        rng.shuffle(rest)
    if rng.random() < 0.25:
        rest = [_typo(t, rng) if not any(c.isdigit() for c in t) else t for t in rest]
    brand = brand.upper() if rng.random() < 0.3 else brand
    out = [brand, *rest]
    if rng.random() < 0.6:
        out.append(rng.choice(MARKETING))
    text = " ".join(out)
    return text.lower() if rng.random() < 0.2 else text


def decoy_name(brand: str, rng: random.Random) -> str:
    return f"{brand} {rng.choice(DECOY_WORDS)} {rng.choice(sorted(TIER_WORDS))} " \
           f"{rng.choice('DEFGHJ')}{rng.randint(10, 99)}"  # fmt: skip


def to_listings(catalog: pd.DataFrame, history: pd.DataFrame, decoy_rate: float = 0.1,
                seed: int = config.RANDOM_SEED) -> pd.DataFrame:  # fmt: skip
    """history: one row per (sku, competitor, date, price, in_stock)."""
    rng = random.Random(seed)
    names = dict(zip(catalog["sku"], catalog["name"]))
    listing_for: dict[tuple[str, str], str] = {}
    rows = []
    for r in history.itertuples(index=False):
        key = (r.sku, r.competitor)
        if key not in listing_for:
            listing_for[key] = noisy_name(names[r.sku], rng)
        rows.append({"competitor": r.competitor, "listing_name": listing_for[key],
                     "price": r.price, "in_stock": int(r.in_stock), "date": r.date,
                     "true_sku": r.sku})  # fmt: skip

    # Decoys: products we don't carry, same brands, priced like real stock.
    dates = sorted(history["date"].unique())
    competitors = sorted(history["competitor"].unique())
    brands = sorted(catalog["brand"].unique())
    n_decoys = int(len(listing_for) * decoy_rate)
    for _ in range(n_decoys):
        comp, name = rng.choice(competitors), decoy_name(rng.choice(brands), rng)
        base = rng.uniform(20, 1500)
        for d in dates:
            rows.append({"competitor": comp, "listing_name": name,
                         "price": round(base * rng.uniform(0.97, 1.03), 2),
                         "in_stock": 1, "date": d, "true_sku": None})  # fmt: skip
    return pd.DataFrame(rows).drop_duplicates(["competitor", "listing_name", "date"])
