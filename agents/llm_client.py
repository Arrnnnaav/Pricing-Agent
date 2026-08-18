"""
Thin wrapper around the Gemini API. This is the ONLY file that talks to
Gemini directly -- every other file that needs an LLM call goes through
generate_structured() here. That isolation means: if we ever swap
models or providers, only this file changes; and every call is
automatically retried and cost-tracked without each caller having to
remember to do it themselves.
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import time
from typing import TypeVar
from pydantic import BaseModel, ValidationError

from google import genai
from google.genai import types

import config

T = TypeVar("T", bound=BaseModel)

_client = genai.Client(api_key=config.GEMINI_API_KEY)

# Gemini 2.0 Flash free-tier pricing (USD per 1M tokens) as of when this
# was written -- approximate, only used for our own cost-guard tracking,
# not billed anywhere. Update these if Google changes pricing.
_INPUT_COST_PER_1M = 0.10
_OUTPUT_COST_PER_1M = 0.40


class RunCostTracker:
    """Tracks total LLM calls and estimated cost for a single pipeline
    run, and enforces the MAX_STEPS_PER_RUN / MAX_COST_USD_PER_RUN
    guards from config.py. One instance is created per run in main.py
    and passed down to whatever needs to make LLM calls.
    """

    def __init__(self):
        self.calls = 0
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.estimated_cost_usd = 0.0

    def record(self, input_tokens: int, output_tokens: int) -> None:
        self.calls += 1
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.estimated_cost_usd += (
            input_tokens / 1_000_000 * _INPUT_COST_PER_1M
            + output_tokens / 1_000_000 * _OUTPUT_COST_PER_1M
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


def generate_structured(
    prompt: str,
    response_model: type[T],
    cost_tracker: RunCostTracker,
    max_retries: int = 2,
) -> T:
    """Calls Gemini with a JSON-schema-constrained prompt and returns a
    validated instance of response_model. Retries on either a Gemini-side
    failure or a Pydantic validation failure (e.g. a hallucinated price
    that trips our field_validator in models.py), since a single bad
    generation shouldn't fail the whole pipeline run.
    """
    cost_tracker.check_within_budget()

    last_error = None
    for attempt in range(1, max_retries + 2):  # +1 initial try, +max_retries
        try:
            response = _client.models.generate_content(
                model=config.GEMINI_MODEL,
                contents=prompt,
                config=types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=response_model,
                    temperature=0.2,
                ),
            )

            usage = response.usage_metadata
            cost_tracker.record(
                input_tokens=usage.prompt_token_count or 0,
                output_tokens=usage.candidates_token_count or 0,
            )

            # response.parsed is the SDK's own schema-validated object;
            # we re-validate through our own model anyway so our custom
            # field_validator (the sane_change check in models.py) runs too.
            return response_model.model_validate(response.parsed.model_dump()
                                                  if hasattr(response.parsed, "model_dump")
                                                  else response.parsed)

        except (ValidationError, ValueError, Exception) as e:
            last_error = e
            if attempt <= max_retries:
                time.sleep(1.5 * attempt)  # brief backoff before retrying
                continue

    raise RuntimeError(
        f"generate_structured failed after {max_retries + 1} attempts. "
        f"Last error: {last_error}"
    )
