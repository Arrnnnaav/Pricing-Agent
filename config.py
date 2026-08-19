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

# --- API key -----------------------------------------------------------
# We read this from an environment variable, never from source code.
# Either put it in a .env file (see .env.example) or run:
#   export GEMINI_API_KEY="your-key-here"
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not GEMINI_API_KEY:
    raise RuntimeError(
        "GEMINI_API_KEY is not set. Get a free key at "
        "https://aistudio.google.com/apikey and run:\n"
        "  export GEMINI_API_KEY='your-key-here'"
    )

# --- Model ---------------------------------------------------------------
# Called by the Decision agent (price recommendations) and the Researcher
# agent (SQL/RAG retrieval). Flash is fast and cheap enough for a daily
# batch job over ~50 SKUs, and supports schema-constrained JSON output
# natively, which is why we picked it over a bigger model.
# NOTE: gemini-2.0-flash and gemini-2.5-flash were both retired by Google
# (confirmed 404 on live calls as of 2026-08-19). Google's own 404 error
# body named gemini-3.6-flash as the replacement -- update this string
# again if it's ever retired too.
GEMINI_MODEL = "gemini-3.6-flash"

# --- Human-in-the-loop ----------------------------------------------------
# Recommendations at or above this confidence auto-execute. Below it,
# they go to the approval gate (CLI today, Slack later).
AUTO_APPROVE_CONFIDENCE_THRESHOLD = 0.90

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

NUM_SKUS = 50
NUM_COMPETITORS = 4
PRICE_HISTORY_DAYS = 7

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
