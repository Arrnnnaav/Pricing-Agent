import json
from unittest.mock import patch

import pytest
from pydantic import BaseModel, Field

import config
from agents.llm_client import LLMCallError, RunCostTracker, generate_structured


class _Suggestion(BaseModel):
    recommended_price: float = Field(gt=0)
    confidence: float = Field(ge=0, le=1)
    reasoning: str


GOOD = json.dumps({"recommended_price": 10.0, "confidence": 0.9, "reasoning": "ok"})


def test_cache_hit_skips_second_call():
    tracker = RunCostTracker()
    prompt = "price this SKU"
    cached = _Suggestion(recommended_price=10.0, confidence=0.5, reasoning="cached")
    tracker.put_cached(prompt, cached)
    with patch("agents.llm_client._call") as call:
        assert generate_structured(prompt, _Suggestion, tracker) == cached
    call.assert_not_called()


def test_invalid_json_is_retried_then_succeeds(monkeypatch):
    monkeypatch.setattr(config, "LLM_MODELS", ["m1"])
    replies = iter([('{"recommended_price": -5}', 5, 5), (GOOD, 5, 5)])
    tracker = RunCostTracker()
    with patch("agents.llm_client._call", side_effect=lambda *a: next(replies)), \
         patch("agents.llm_client.time.sleep"):  # fmt: skip
        out = generate_structured("p", _Suggestion, tracker)
    assert out.recommended_price == 10.0
    assert tracker.retries == 1 and tracker.fallback_model_used == 0


def test_next_model_used_and_counted_when_primary_fails(monkeypatch):
    monkeypatch.setattr(config, "LLM_MODELS", ["bad", "good"])

    def call(model, prompt, schema):
        if model == "bad":
            raise LLMCallError("HTTP 500")
        return GOOD, 5, 5

    tracker = RunCostTracker()
    with patch("agents.llm_client._call", side_effect=call), \
         patch("agents.llm_client.time.sleep"):  # fmt: skip
        generate_structured("p", _Suggestion, tracker, max_retries=1)
    assert tracker.fallback_model_used == 1
    assert tracker.stats()["models_used"] == {"good": 1}


def test_rule_fallback_used_and_counted_when_all_models_fail(monkeypatch):
    monkeypatch.setattr(config, "LLM_MODELS", ["m1", "m2"])
    tracker = RunCostTracker()
    fallback = _Suggestion(recommended_price=99.0, confidence=0.5, reasoning="fallback")
    with patch("agents.llm_client._call", side_effect=LLMCallError("down")), \
         patch("agents.llm_client.time.sleep"):  # fmt: skip
        result = generate_structured("anything", _Suggestion, tracker, max_retries=1,
                                     fallback_factory=lambda: fallback)  # fmt: skip
    assert result == fallback
    assert tracker.rule_fallback_used == 1 and tracker.generate_successes == 1


def test_raises_without_fallback(monkeypatch):
    monkeypatch.setattr(config, "LLM_MODELS", ["m1"])
    with patch("agents.llm_client._call", side_effect=LLMCallError("down")), \
         patch("agents.llm_client.time.sleep"):  # fmt: skip
        with pytest.raises(RuntimeError):
            generate_structured("p", _Suggestion, RunCostTracker(), max_retries=0)
