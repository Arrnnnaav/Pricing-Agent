"""
Thin, provider-agnostic LLM wrapper. This is the ONLY file that talks to a
model directly -- every other file that needs an LLM call goes through
generate_structured() here. Swapping providers or models changes only
this file and config.py; every call is automatically schema-constrained,
validated, retried, walked down a model fallback chain, and cost-tracked.

Providers:
  ollama         local model, JSON-schema-constrained output via `format`
                 (default: no key, no quota, $0 per call)
  openai_compat  OpenRouter / NVIDIA NIM / any /chat/completions gateway,
                 JSON mode + schema in the prompt
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import time
from typing import TypeVar

import httpx
from pydantic import BaseModel, ValidationError

import config

T = TypeVar("T", bound=BaseModel)

_http = httpx.Client(timeout=config.LLM_TIMEOUT_S)


class LLMCallError(RuntimeError):
    pass


class RunCostTracker:
    """Tracks total LLM calls and estimated cost for a single pipeline
    run, and enforces the MAX_STEPS_PER_RUN / MAX_COST_USD_PER_RUN
    guards from config.py. One instance is created per run and passed
    down to whatever needs to make LLM calls.
    """

    def __init__(self):
        self.calls = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.estimated_cost_usd = 0.0
        self._cache: dict[str, BaseModel] = {}
        # One increment per generate_structured() call (not per retry) --
        # used by audit.metrics.compute_run_metrics to compute a real
        # schema_success_rate (successes / attempts).
        self.generate_attempts = 0
        self.generate_successes = 0
        # Degraded paths, so the audit log can answer "how often did we
        # fall back?" instead of hiding it inside a successful result.
        self.fallback_model_used = 0  # primary model failed, a later one answered
        self.rule_fallback_used = 0  # every model failed, fallback_factory answered
        self.retries = 0
        self.latencies_ms: list[int] = []
        self.models_used: dict[str, int] = {}

    def record(self, input_tokens: int, output_tokens: int) -> None:
        self.calls += 1
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.estimated_cost_usd += (
            input_tokens / 1_000_000 * config.LLM_INPUT_COST_PER_1M
            + output_tokens / 1_000_000 * config.LLM_OUTPUT_COST_PER_1M
        )

    def check_within_budget(self) -> None:
        """Raises if this run has exceeded its step or cost ceiling.
        Called before every LLM call so a runaway loop stops immediately
        rather than after it's already blown the budget."""
        if self.calls >= config.MAX_STEPS_PER_RUN:
            raise RuntimeError(
                f"MAX_STEPS_PER_RUN ({config.MAX_STEPS_PER_RUN}) exceeded -- "
                f"stopping run to avoid a runaway loop."
            )
        if self.estimated_cost_usd >= config.MAX_COST_USD_PER_RUN:
            raise RuntimeError(
                f"MAX_COST_USD_PER_RUN (${config.MAX_COST_USD_PER_RUN:.2f}) "
                f"exceeded -- stopping run."
            )

    def get_cached(self, prompt: str):
        return self._cache.get(prompt)

    def put_cached(self, prompt: str, result) -> None:
        self._cache[prompt] = result

    def stats(self) -> dict:
        lat = sorted(self.latencies_ms)
        return {
            "generate_attempts": self.generate_attempts,
            "generate_successes": self.generate_successes,
            "llm_calls": self.calls,
            "retries": self.retries,
            "fallback_model_used": self.fallback_model_used,
            "rule_fallback_used": self.rule_fallback_used,
            "models_used": self.models_used,
            "input_tokens": self.total_input_tokens,
            "output_tokens": self.total_output_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "latency_p50_ms": lat[len(lat) // 2] if lat else None,
        }


def _call_ollama(model: str, prompt: str, schema: dict) -> tuple[str, int, int]:
    r = _http.post(
        f"{config.OLLAMA_URL.rstrip('/')}/api/chat",
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "format": schema,  # server-side JSON-schema-constrained decoding
            "stream": False,
            "options": {"temperature": 0.2},
        },
    )
    if r.status_code >= 400:
        raise LLMCallError(f"ollama HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    return (
        data.get("message", {}).get("content", ""),
        data.get("prompt_eval_count", 0),
        data.get("eval_count", 0),
    )


def _call_openai_compat(model: str, prompt: str, schema: dict) -> tuple[str, int, int]:
    r = _http.post(
        f"{config.OPENAI_COMPAT_BASE_URL.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {config.OPENAI_COMPAT_API_KEY}"},
        json={
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": f"{prompt}\n\nRespond with JSON only, matching this schema:\n"
                    f"{json.dumps(schema)}",
                }
            ],
            "response_format": {"type": "json_object"},
            "temperature": 0.2,
        },
    )
    if r.status_code >= 400:
        raise LLMCallError(f"HTTP {r.status_code}: {r.text[:200]}")
    data = r.json()
    usage = data.get("usage") or {}
    content = (data.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    return content, usage.get("prompt_tokens", 0), usage.get("completion_tokens", 0)


def _call(model: str, prompt: str, schema: dict) -> tuple[str, int, int]:
    """Single provider call; the seam tests patch."""
    if config.LLM_PROVIDER == "openai_compat":
        return _call_openai_compat(model, prompt, schema)
    return _call_ollama(model, prompt, schema)


def generate_structured(
    prompt: str,
    response_model: type[T],
    cost_tracker: RunCostTracker,
    max_retries: int = 2,
    fallback_factory=None,
) -> T:
    """Returns a validated instance of response_model.

    Per model in config.LLM_MODELS: up to max_retries+1 attempts, retrying
    on transport errors and on Pydantic validation failures (e.g. a
    hallucinated price that trips a field_validator in models.py). Then
    the next model. If every model fails, fallback_factory() (a
    deterministic rule) answers and the degraded path is counted.

    Results are cached by prompt string, so identical prompts within the
    same run skip the LLM call entirely.
    """
    cost_tracker.generate_attempts += 1

    cached = cost_tracker.get_cached(prompt)
    if cached is not None:
        cost_tracker.generate_successes += 1
        return cached

    schema = response_model.model_json_schema()
    last_error = None
    for i, model in enumerate(config.LLM_MODELS):
        for attempt in range(1, max_retries + 2):  # +1 initial try, +max_retries
            cost_tracker.check_within_budget()
            started = time.perf_counter()
            try:
                content, tokens_in, tokens_out = _call(model, prompt, schema)
                cost_tracker.record(tokens_in, tokens_out)
                cost_tracker.latencies_ms.append(
                    int((time.perf_counter() - started) * 1000)
                )
                result = response_model.model_validate_json(content)
            except (ValidationError, ValueError, httpx.HTTPError, LLMCallError) as e:
                last_error = e
                if attempt <= max_retries:
                    cost_tracker.retries += 1
                    time.sleep(0.5 * attempt)  # brief backoff before retrying
                continue
            cost_tracker.put_cached(prompt, result)
            cost_tracker.generate_successes += 1
            cost_tracker.models_used[model] = cost_tracker.models_used.get(model, 0) + 1
            if i > 0:
                cost_tracker.fallback_model_used += 1
            return result

    if fallback_factory is not None:
        # A fallback is still a successful call from the caller's
        # perspective (degraded, but not a raised exception); it is
        # counted separately so the audit log shows how often it fired.
        result = fallback_factory()
        cost_tracker.put_cached(prompt, result)
        cost_tracker.generate_successes += 1
        cost_tracker.rule_fallback_used += 1
        return result

    raise RuntimeError(
        f"generate_structured failed on all models {config.LLM_MODELS}. "
        f"Last error: {last_error}"
    )
