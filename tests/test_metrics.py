import os
import tempfile
from unittest.mock import patch
import config
from audit.logger import log_event
from audit.metrics import compute_run_metrics


def test_compute_run_metrics_from_audit_log():
    with tempfile.TemporaryDirectory() as tmp:
        audit_path = os.path.join(tmp, "audit.jsonl")
        with (
            patch.object(config, "AUDIT_LOG_PATH", audit_path),
            patch.object(config, "AUDIT_DIR", tmp),
        ):
            log_event(
                "run1",
                "decision",
                "batch_complete",
                {
                    "recommendations": [
                        {
                            "sku": "A",
                            "recommended_price": 100.0,
                            "reasoning": "matches trend",
                        },
                        {
                            "sku": "B",
                            "recommended_price": 500.0,
                            "reasoning": "no reason given",
                        },
                    ]
                },
            )
            log_event(
                "run1", "tool", "optimizer", {"success": True, "latency_ms": 12.0}
            )
            log_event(
                "run1", "tool", "optimizer", {"success": False, "latency_ms": 5.0}
            )

            metrics = compute_run_metrics("run1")
            assert metrics["tool_success_rate"] == 0.5
            assert "schema_success_rate" in metrics
            assert "hallucination_flags" in metrics
