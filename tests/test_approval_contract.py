from unittest.mock import patch
from models import PriceRecommendation
from agents.approval import route_recommendation


def _rec(confidence=0.5):
    return PriceRecommendation(
        sku="LAP-0001",
        current_price=100.0,
        recommended_price=105.0,
        confidence=confidence,
        reasoning="test",
        projected_margin_pct=0.3,
    )


def test_low_confidence_routes_through_configured_asker():
    with patch("agents.approval._ask_human", return_value=True) as mock_ask:
        outcome = route_recommendation(_rec(confidence=0.5))
    mock_ask.assert_called_once()
    assert outcome.value == "human_approved"


def test_high_confidence_auto_approves_without_asking():
    with patch("agents.approval._ask_human") as mock_ask:
        outcome = route_recommendation(_rec(confidence=0.95))
    mock_ask.assert_not_called()
    assert outcome.value == "auto_approved"
