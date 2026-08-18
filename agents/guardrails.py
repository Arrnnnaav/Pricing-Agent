"""
Pure guardrail functions -- the hard business rules a price
recommendation must never violate, regardless of how confident the LLM
is. These are deliberately kept free of any LLM call, file I/O, or
network dependency, so they can be tested in complete isolation with
plain numbers in, an enum out.

Used by analyst.py (to flag SKUs before they reach the Decision agent)
and decision.py (to double-check the LLM's own recommendation before
it's allowed to reach the approval gate).
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config
from models import CatalogItem, GuardrailViolation


def check_margin_floor(item: CatalogItem, candidate_price: float) -> bool:
    """True if candidate_price keeps margin at or above MIN_MARGIN_PCT."""
    margin = (candidate_price - item.cost) / candidate_price
    return margin >= config.MIN_MARGIN_PCT


def check_max_price_change(current_price: float, candidate_price: float) -> bool:
    """True if the change from current to candidate stays within
    MAX_PRICE_CHANGE_PCT in either direction."""
    change_pct = abs(candidate_price - current_price) / current_price
    return change_pct <= config.MAX_PRICE_CHANGE_PCT


def check_stock_allows_raise(item: CatalogItem, candidate_price: float) -> bool:
    """Blocks price *increases* when stock is low. A price cut is always
    allowed regardless of stock -- only raises are restricted, since
    raising the price on something you're nearly out of doesn't reflect
    a genuine demand signal, it just looks like profiteering on scarcity."""
    is_raise = candidate_price > item.our_price
    if not is_raise:
        return True
    return item.stock > config.LOW_STOCK_THRESHOLD_UNITS


def evaluate_guardrails(item: CatalogItem, candidate_price: float) -> GuardrailViolation:
    """Runs every guardrail check in order and returns the first
    violation found, or GuardrailViolation.NONE if the candidate price
    passes all of them. Order matters only for which violation gets
    reported first when multiple rules are broken at once -- margin is
    checked first since it's the most fundamental business constraint.
    """
    if not check_margin_floor(item, candidate_price):
        return GuardrailViolation.BELOW_MIN_MARGIN

    if not check_max_price_change(item.our_price, candidate_price):
        return GuardrailViolation.EXCEEDS_MAX_PRICE_CHANGE

    if not check_stock_allows_raise(item, candidate_price):
        return GuardrailViolation.LOW_STOCK_BLOCKS_RAISE

    return GuardrailViolation.NONE
