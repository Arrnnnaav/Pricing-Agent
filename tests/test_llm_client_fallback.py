from unittest.mock import patch
from pydantic import BaseModel
from agents.llm_client import RunCostTracker, generate_structured


class _Suggestion(BaseModel):
    recommended_price: float
    confidence: float
    reasoning: str


def test_cache_hit_skips_second_call():
    tracker = RunCostTracker()
    prompt = "price this SKU"
    cached = _Suggestion(recommended_price=10.0, confidence=0.5, reasoning="cached")
    tracker.put_cached(prompt, cached)
    assert tracker.get_cached(prompt) == cached


def test_fallback_used_when_all_retries_fail():
    tracker = RunCostTracker()
    fallback = _Suggestion(recommended_price=99.0, confidence=0.5, reasoning="fallback")

    with patch(
        "agents.llm_client._client.models.generate_content",
        side_effect=RuntimeError("boom"),
    ):
        result = generate_structured(
            prompt="anything",
            response_model=_Suggestion,
            cost_tracker=tracker,
            max_retries=1,
            fallback_factory=lambda: fallback,
        )
    assert result == fallback
