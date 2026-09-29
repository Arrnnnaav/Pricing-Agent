"""Estimates per-category demand elasticity from sales history.

Model (the same linear form the optimizer uses):
    units_it = a_i + b_i * gap_it,   gap_it = (p_it - m_it) / m_it

a_i is SKU i's demand at the market price and b_i = a_i * e_i, so each
SKU gives e_i = b_i / a_i from its own OLS fit. The category estimate is
the median across its SKUs (robust to a few noisy low-volume SKUs).

Two biases the first version had, now removed:
  * Stock-capped days report min(demand, stock), flattening the price
    response, so they are excluded.
  * Normalizing by mean units and fitting through the origin is biased
    when a SKU sits persistently above/below the market (mean gap != 0);
    a per-SKU intercept avoids that.
Needs price variation to identify slopes, which is why the warmup period
applies small random price experiments.
"""

from __future__ import annotations

import json

import numpy as np
import pandas as pd


def estimate(sales: pd.DataFrame, min_obs: int = 8) -> dict[str, float]:
    """sales columns: sku, category, gap, units, capped (bool)."""
    df = sales[~sales.get("capped", False)] if "capped" in sales else sales
    per_sku = []
    for (sku, cat), g in df.groupby(["sku", "category"]):
        if len(g) < min_obs or g["gap"].std() < 1e-4 or g["units"].mean() < 1:
            continue
        b, a = np.polyfit(g["gap"].to_numpy(), g["units"].to_numpy(), 1)
        if a > 0:
            per_sku.append((cat, b / a))
    est = pd.DataFrame(per_sku, columns=["category", "e"])
    return {c: round(float(g["e"].median()), 3) for c, g in est.groupby("category")}


def save(estimates: dict[str, float], path: str) -> None:
    with open(path, "w") as f:
        json.dump(estimates, f, indent=2)
