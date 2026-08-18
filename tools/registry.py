"""Formal tool registry: every tool the Decision agent can call goes
through here so input/output validation and an audit trail are
guaranteed, not something each tool remembers to do itself. This is
the project's stand-in for MCP-style tool-call governance without
standing up a real MCP server (see the design spec's iteration-3
section for why).
"""

import time
from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel

from audit.logger import log_event


@dataclass
class Tool:
    name: str
    input_model: type[BaseModel]
    output_model: type[BaseModel]
    func: Callable[[BaseModel], BaseModel]


_REGISTRY: dict[str, Tool] = {}


def register(tool: Tool) -> None:
    _REGISTRY[tool.name] = tool


def call_tool(name: str, input_dict: dict, run_id: str = "unlogged") -> BaseModel:
    if name not in _REGISTRY:
        raise KeyError(f"No tool registered under name '{name}'")

    tool = _REGISTRY[name]
    validated_input = tool.input_model.model_validate(input_dict)

    start = time.monotonic()
    success = True
    try:
        result = tool.func(validated_input)
        validated_output = tool.output_model.model_validate(result.model_dump())
        return validated_output
    except Exception:
        success = False
        raise
    finally:
        latency_ms = round((time.monotonic() - start) * 1000, 1)
        log_event(
            run_id,
            "tool",
            name,
            {
                "input": input_dict,
                "success": success,
                "latency_ms": latency_ms,
            },
        )


def register_all_tools() -> None:
    """Registers every tool the Decision agent can call. Imports are
    local to avoid a circular import (optimizer/semantic_matcher/scraper
    don't need to import registry at module load time otherwise)."""
    from tools.optimizer import (
        OptimizerInput,
        OptimizerOutput,
        optimize_category_prices,
    )
    from tools.semantic_matcher import MatchInput, MatchOutput, match_listing_to_sku
    from tools.scraper_tool import ScrapeInput, ScrapeOutput, scrape_competitor_prices

    register(
        Tool("optimizer", OptimizerInput, OptimizerOutput, optimize_category_prices)
    )
    register(Tool("semantic_matcher", MatchInput, MatchOutput, match_listing_to_sku))
    register(Tool("scraper", ScrapeInput, ScrapeOutput, scrape_competitor_prices))
