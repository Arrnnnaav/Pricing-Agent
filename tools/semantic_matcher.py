"""Fuzzy-matches a competitor listing's free-text product name to one
of our SKUs. Competitor data won't always arrive pre-tagged with our
SKU codes -- a real scraper sees "dell vortex PRO 8GB (2026 edition)" on a
competitor site and has to figure out that's our LAP-0001.

Two stages, like record linkage in production:
  1. Blocking: if a known brand appears in the listing, only that brand's
     SKUs are candidates. Cuts comparisons ~10x and stops a strong model
     word from matching the wrong brand.
  2. Scoring: rapidfuzz token_set_ratio (order-insensitive, tolerant of
     extra marketing words), with a bonus when the model number matches
     exactly -- the most discriminative token in an electronics name.
A listing below the threshold is left unmatched rather than guessed.
"""

import re

from pydantic import BaseModel
from rapidfuzz import fuzz, process, utils

_MATCH_THRESHOLD = 80.0
_MODEL_NO = re.compile(r"\b[a-z]\d{2}\b")


class MatchInput(BaseModel):
    listing_name: str
    candidate_skus: dict[str, str]  # sku -> catalog product name


class MatchOutput(BaseModel):
    matched_sku: str | None
    score: float


def _model_numbers(text: str) -> set[str]:
    return set(_MODEL_NO.findall(text.lower()))


def _brand_block(listing: str, candidates: dict[str, str]) -> dict[str, str]:
    words = set(utils.default_process(listing).split())
    by_brand: dict[str, dict[str, str]] = {}
    for sku, name in candidates.items():
        brand = name.split()[0].lower()
        by_brand.setdefault(brand, {})[sku] = name
    for brand, skus in by_brand.items():
        if brand in words:
            return skus
    return candidates


def match_listing_to_sku(inp: MatchInput) -> MatchOutput:
    pool = _brand_block(inp.listing_name, inp.candidate_skus)
    listing_models = _model_numbers(inp.listing_name)
    best = process.extract(
        inp.listing_name,
        pool,
        scorer=fuzz.token_set_ratio,
        processor=utils.default_process,
        limit=5,
    )
    best_sku, best_score = None, 0.0
    for name, score, sku in best:
        if listing_models and listing_models & _model_numbers(name):
            score = min(100.0, score + 10)
        elif listing_models:
            score -= 15  # names a different model number: likely a sibling product
        if score > best_score:
            best_sku, best_score = sku, float(score)

    if best_score < _MATCH_THRESHOLD:
        return MatchOutput(matched_sku=None, score=best_score)
    return MatchOutput(matched_sku=best_sku, score=best_score)
