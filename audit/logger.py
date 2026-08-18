"""
Audit logger: appends one JSON line per pipeline event to
audit/agent_audit.jsonl. JSONL (one JSON object per line) instead of a
single JSON array, because it's append-only -- writing a new event never
requires reading and rewriting the whole file, which matters once this
log has months of daily runs in it.
"""

import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
from datetime import datetime, timezone
from typing import Any

import config


def log_event(
    run_id: str, agent: str, event: str, data: dict[str, Any], latency_ms: float = None
) -> None:
    """Appends one audit event. `data` should already be JSON-serializable
    (e.g. from calling .model_dump(mode="json") on a Pydantic model) --
    this function doesn't try to serialize arbitrary Python objects for you.
    """
    os.makedirs(config.AUDIT_DIR, exist_ok=True)

    entry = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "run_id": run_id,
        "agent": agent,
        "event": event,
        "data": data,
    }
    if latency_ms is not None:
        entry["latency_ms"] = latency_ms

    with open(config.AUDIT_LOG_PATH, "a") as f:
        f.write(json.dumps(entry, default=str) + "\n")


def read_run(run_id: str) -> list[dict]:
    """Reads back every event for a given run_id -- useful for debugging
    a specific run or building a summary report after the fact."""
    if not os.path.exists(config.AUDIT_LOG_PATH):
        return []

    events = []
    with open(config.AUDIT_LOG_PATH) as f:
        for line in f:
            entry = json.loads(line)
            if entry["run_id"] == run_id:
                events.append(entry)
    return events
