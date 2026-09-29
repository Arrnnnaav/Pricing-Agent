"""Synthetic market for backtesting: competitor price paths plus a hidden
demand model that the pricing strategies never see directly.

True demand for SKU i on day t at our price p:

    units = base_i * max(0, 1 + e_i * (p - m_t) / m_t)

where m_t is the mean in-stock competitor price that day and e_i is the
SKU's true elasticity: its category's mean plus per-SKU noise. The
optimizer only gets a per-category estimate learned from sales history
(simulation/elasticity.py), never e_i, so model error is part of the test.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

from data.mock_price_generator import COMPETITOR_PROFILES, COMPETITORS, generate_walk

TRUE_CATEGORY_ELASTICITY = {
    "Laptops": -2.0, "Smartphones": -2.4, "Headphones": -2.8, "Smartwatches": -2.2,
    "Tablets": -2.0, "Monitors": -1.8, "Accessories": -3.0,
}  # fmt: skip


@dataclass
class Market:
    catalog: pd.DataFrame  # sku, category, cost, our_price, stock, ...
    days: int
    comp_prices: np.ndarray  # [sku, competitor, day]
    comp_in_stock: np.ndarray  # [sku, competitor, day] bool
    elasticity: np.ndarray  # [sku] true elasticity
    base_demand: np.ndarray  # [sku] units/day at the market price

    def market_price(self, day: int) -> np.ndarray:
        p = np.where(self.comp_in_stock[:, :, day], self.comp_prices[:, :, day], np.nan)
        m = np.nanmean(p, axis=1)
        return np.where(np.isnan(m), self.comp_prices[:, :, day].mean(axis=1), m)

    def expected_units(self, prices: np.ndarray, day: int) -> np.ndarray:
        m = self.market_price(day)
        units = self.base_demand * np.maximum(
            0.0, 1 + self.elasticity * (prices - m) / m
        )
        return np.minimum(units, self.catalog["stock"].to_numpy())  # daily availability


def build_market(catalog: pd.DataFrame, days: int, seed: int = 7) -> Market:
    rng = np.random.default_rng(seed)
    np.random.seed(seed)  # generate_walk draws from numpy's global RNG
    n, k = len(catalog), len(COMPETITORS)
    comp = np.zeros((n, k, days))
    stock = np.ones((n, k, days), dtype=bool)
    for i, price in enumerate(catalog["our_price"]):
        for j, name in enumerate(COMPETITORS):
            prof = COMPETITOR_PROFILES[name]
            comp[i, j] = generate_walk(round(price * prof["price_bias"], 2), prof, days)
            stock[i, j] = rng.random(days) > prof["stockout_prob"]
    cat_e = catalog["category"].map(TRUE_CATEGORY_ELASTICITY).to_numpy()
    elasticity = cat_e + rng.normal(0, 0.3, n)
    # Cheaper items sell more units; lognormal spread across SKUs.
    base = np.exp(
        rng.normal(np.log(12) - 0.35 * np.log(catalog["our_price"] / 100), 0.4)
    )
    return Market(catalog.reset_index(drop=True), days, comp, stock, elasticity, base)
