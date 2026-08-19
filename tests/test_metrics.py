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


def test_schema_success_rate_is_a_real_ratio():
    with tempfile.TemporaryDirectory() as tmp:
        audit_path = os.path.join(tmp, "audit.jsonl")
        with (
            patch.object(config, "AUDIT_LOG_PATH", audit_path),
            patch.object(config, "AUDIT_DIR", tmp),
        ):
            log_event(
                "run2",
                "decision",
                "llm_stats",
                {"generate_attempts": 10, "generate_successes": 7},
            )

            metrics = compute_run_metrics("run2")
            assert metrics["schema_success_rate"] == 0.7


def test_schema_success_rate_is_none_with_no_attempts():
    with tempfile.TemporaryDirectory() as tmp:
        audit_path = os.path.join(tmp, "audit.jsonl")
        with (
            patch.object(config, "AUDIT_LOG_PATH", audit_path),
            patch.object(config, "AUDIT_DIR", tmp),
        ):
            log_event("run3", "decision", "batch_complete", {"recommendations": []})

            metrics = compute_run_metrics("run3")
            assert metrics["schema_success_rate"] is None


def test_hallucination_requires_both_out_of_range_price_and_no_real_trend():
    with tempfile.TemporaryDirectory() as tmp:
        audit_path = os.path.join(tmp, "audit.jsonl")
        with (
            patch.object(config, "AUDIT_LOG_PATH", audit_path),
            patch.object(config, "AUDIT_DIR", tmp),
        ):
            log_event(
                "run4",
                "decision",
                "batch_complete",
                {
                    "recommendations": [
                        # Optimizer-style recommendation: templated
                        # reasoning, but the price is actually inside the
                        # competitor range -- should NOT be flagged.
                        {
                            "sku": "IN-RANGE",
                            "recommended_price": 100.0,
                            "reasoning": "LP-optimized category price.",
                            "min_competitor_price": 90.0,
                            "max_competitor_price": 110.0,
                        },
                        # Price is outside the range, but a real trend
                        # (5%, well above the noise threshold) justifies
                        # it -- should NOT be flagged. This is the case
                        # the old lexical "did it say the word trend"
                        # check couldn't distinguish from a hallucination.
                        {
                            "sku": "REAL-TREND",
                            "recommended_price": 500.0,
                            "reasoning": "Competitor prices are rising steadily.",
                            "min_competitor_price": 90.0,
                            "max_competitor_price": 110.0,
                            "avg_competitor_trend_pct": 0.05,
                        },
                        # True hallucination: price is outside the range
                        # AND the actual trend is noise (0.1%, below
                        # threshold) even though the reasoning claims a
                        # trend justifies it -- should be flagged. This is
                        # exactly what the lexical check would have missed.
                        {
                            "sku": "FAKE-TREND",
                            "recommended_price": 500.0,
                            "reasoning": "The trend suggests a strong increase.",
                            "min_competitor_price": 90.0,
                            "max_competitor_price": 110.0,
                            "avg_competitor_trend_pct": 0.001,
                        },
                        # True hallucination: price outside range, no
                        # trend data at all -- should be flagged.
                        {
                            "sku": "NO-TREND-DATA",
                            "recommended_price": 500.0,
                            "reasoning": "LP-optimized category price.",
                            "min_competitor_price": 90.0,
                            "max_competitor_price": 110.0,
                        },
                    ]
                },
            )

            metrics = compute_run_metrics("run4")
            assert metrics["hallucination_flags"] == ["FAKE-TREND", "NO-TREND-DATA"]
