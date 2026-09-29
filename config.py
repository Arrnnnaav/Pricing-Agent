"""
Central configuration for the Pricing Intelligence Agent.

Every other file in this project imports settings from here instead of
hardcoding numbers or reading environment variables directly. That means
if you ever need to change the confidence threshold, or swap models, you
change it in exactly one place.
"""

import os
from dotenv import load_dotenv

# Reads a .env file in the project root (if one exists) and loads its
# key=value pairs into the environment. If no .env file exists, this
# does nothing and we fall through to a real environment variable
# (useful for CI/deployment, where you wouldn't ship a .env file at all).
load_dotenv()

# --- LLM provider ----------------------------------------------------------
# Only the Decision agent's single-SKU path calls an LLM (multi-SKU
# categories go through the LP optimizer, no LLM). Default is a local model
# via Ollama: no API key, no daily quota, $0 per call, and pricing data never
# leaves the machine. Any OpenAI-compatible gateway (OpenRouter free models,
# NVIDIA NIM) can be swapped in with LLM_PROVIDER=openai_compat.
#   ollama pull qwen3:4b-instruct
LLM_PROVIDER = os.environ.get("LLM_PROVIDER", "ollama")  # ollama | openai_compat
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
# First model is primary; the rest are a fallback chain tried in order.
LLM_MODELS = [
    m.strip()
    for m in os.environ.get("LLM_MODELS", "qwen3:4b-instruct").split(",")
    if m.strip()
]
OPENAI_COMPAT_BASE_URL = os.environ.get("OPENAI_COMPAT_BASE_URL", "https://openrouter.ai/api/v1")
OPENAI_COMPAT_API_KEY = os.environ.get("OPENAI_COMPAT_API_KEY", "")
LLM_TIMEOUT_S = float(os.environ.get("LLM_TIMEOUT_S", "120"))

# Cost tracking for the cost guard. Local inference is free; set these to a
# hosted model's per-1M-token prices when using openai_compat.
LLM_INPUT_COST_PER_1M = float(os.environ.get("LLM_INPUT_COST_PER_1M", "0"))
LLM_OUTPUT_COST_PER_1M = float(os.environ.get("LLM_OUTPUT_COST_PER_1M", "0"))

# --- Human-in-the-loop ----------------------------------------------------
# Recommendations at or above this confidence auto-execute. Below it,
# they go to the approval gate (CLI today, Slack later).
AUTO_APPROVE_CONFIDENCE_THRESHOLD = 0.90

# LP-optimizer recommendations: a change of at most this size (and
# guardrail-clean, reversible) is routine and auto-executes; anything larger
# is escalated to a human even though the optimizer is deterministic.
OPTIMIZER_AUTO_MAX_CHANGE_PCT = 0.05
OPTIMIZER_SMALL_CHANGE_CONFIDENCE = 0.92
OPTIMIZER_LARGE_CHANGE_CONFIDENCE = 0.85

# How long to wait for a human response before treating it as a timeout.
APPROVAL_TIMEOUT_SECONDS = 300  # 5 minutes

# --- Guardrails (business rules the Decision agent must never violate) ---
# Absolute floor: a recommended price can never drop margin below this,
# no matter how confident the model is or how aggressive competitors are.
MIN_MARGIN_PCT = 0.15

# A single price change can't move more than this in either direction.
# Catches hallucinated or nonsensical recommendations before they reach
# the approval gate.
MAX_PRICE_CHANGE_PCT = 0.25

# If stock is at or below this, we block price *increases* on that SKU
# (raising the price on something you're about to run out of looks bad
# and doesn't reflect a real demand signal).
LOW_STOCK_THRESHOLD_UNITS = 10

# --- Cost / safety guards (production reliability pattern) ---------------
# Hard ceiling on LLM calls and spend for a single pipeline run. If either
# is hit, the run stops and logs why, instead of looping indefinitely.
MAX_STEPS_PER_RUN = 100
MAX_COST_USD_PER_RUN = 1.00

# --- Synthetic data ---------------------------------------------------
# Fixed seed so the generated catalog and price history are reproducible
# across runs — useful for demos and for writing tests against known output.
RANDOM_SEED = 42

NUM_SKUS = int(os.environ.get("NUM_SKUS", "500"))
NUM_COMPETITORS = 4
PRICE_HISTORY_DAYS = 14

# How much a competitor's price is allowed to jitter day-to-day, as a
# fraction of the previous day's price (used by the random-walk generator).
DAILY_PRICE_STEP_STD_PCT = 0.015

# --- File paths ------------------------------------------------------
# Anchored to this file's own location (the project root), not to
# whatever directory you happen to run a script from. Relative paths
# like "data/catalog.csv" only work if you always run from the project
# root -- one script executed from inside data/ or agents/ would look
# in the wrong place. Anchoring to __file__ makes every path correct
# regardless of your current working directory.
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))

DATA_DIR = os.path.join(PROJECT_ROOT, "data")
DB_PATH = os.path.join(DATA_DIR, "pricing_agent.db")

AUDIT_DIR = os.path.join(PROJECT_ROOT, "audit")
AUDIT_LOG_PATH = os.path.join(AUDIT_DIR, "agent_audit.jsonl")

# --- Slack approval gate (optional) -------------------------------------------
# If SLACK_BOT_TOKEN is not set, the approval gate falls back to CLI.
# Otherwise, approval requests are posted to Slack with Approve/Reject buttons.
SLACK_BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN")
SLACK_SIGNING_SECRET = os.environ.get("SLACK_SIGNING_SECRET")
SLACK_APPROVAL_CHANNEL = os.environ.get("SLACK_APPROVAL_CHANNEL", "#pricing-approvals")

# How recommendations that need a human are handled:
#   queue - non-blocking: written to the pending_approvals table and the run
#           continues (default; right for a scheduled batch job)
#   cli   - blocking terminal prompt (interactive demos)
#   slack - Slack Block Kit buttons (requires SLACK_BOT_TOKEN)
APPROVAL_MODE = os.environ.get(
    "APPROVAL_MODE", "slack" if SLACK_BOT_TOKEN else "queue"
)


# --- Demand elasticity -------------------------------------------------------
# Used by the LP optimizer. simulation/elasticity.py estimates these per
# category from sales history and writes data/elasticity.json; until then a
# conservative default is used.
DEFAULT_ELASTICITY = -1.5
ELASTICITY_PATH = os.path.join(DATA_DIR, "elasticity.json")


def category_elasticity(category: str) -> float:
    import json

    try:
        with open(ELASTICITY_PATH) as f:
            return float(json.load(f).get(category, DEFAULT_ELASTICITY))
    except (OSError, ValueError):
        return DEFAULT_ELASTICITY
