"""Fuzzy-matches a competitor listing's free-text product name to one
of our SKUs. Competitor data won't always arrive pre-tagged with our
SKU codes -- a real scraper sees "Dell Vortex Pro 8GB" on a competitor
site and has to figure out that's our LAP-0001, not receive it labeled
as such."""

from pydantic import BaseModel
from rapidfuzz import fuzz

_MATCH_THRESHOLD = 60.0


class MatchInput(BaseModel):
    listing_name: str
    candidate_skus: dict[str, str]  # sku -> catalog product name


class MatchOutput(BaseModel):
    matched_sku: str | None
    score: float


def match_listing_to_sku(inp: MatchInput) -> MatchOutput:
    best_sku, best_score = None, 0.0
    for sku, name in inp.candidate_skus.items():
        score = fuzz.token_sort_ratio(inp.listing_name, name)
        if score > best_score:
            best_sku, best_score = sku, score

    if best_score < _MATCH_THRESHOLD:
        return MatchOutput(matched_sku=None, score=best_score)
    return MatchOutput(matched_sku=best_sku, score=best_score)
