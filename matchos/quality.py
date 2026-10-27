"""Measured scorecards from authored evaluations; never infer missing measurements."""

from __future__ import annotations

import math


def ratio(passed, total):
    return {"passed": passed, "evaluated": total, "rate": passed / total if total else None}


def percentile(values, fraction):
    values = sorted(value for value in values if isinstance(value, (int, float)) and math.isfinite(value))
    return values[max(0, math.ceil(len(values) * fraction) - 1)] if values else None


def grading_summary(grading):
    summary = {key: grading.get(key) for key in ("status", "run_id", "judge_model", "report_url")}
    summary["result_counts"] = grading.get("result", {}).get("result_counts")
    values = {}
    for item in grading.get("output_items", []):
        for result in item.get("results", []):
            name, score = result.get("name"), result.get("score")
            if (
                name in {"editorial_selection", "persona_quality", "semantic_grounding", "restraint"}
                and type(score) in {int, float}
                and math.isfinite(score)
                and 0 <= score <= 1
            ):
                values.setdefault(name, []).append(score)
    summary["scores"] = {
        name: {"mean": sum(scores) / len(scores), "evaluated": len(scores)} for name, scores in values.items()
    }
    return summary


def scorecard(report):
    rows = report["rows"]
    aired = [row for row in rows if row["publication"] == "AIR"]
    repeats = [
        row
        for row in rows
        if row["case_id"] in {"recent-update", "goal-already-aired", "dismissal-already-aired"}
    ]
    quiet = [row for row in rows if row["case_id"] == "quiet"]
    expired = [row for row in rows if row["case_id"] == "expired"]
    calls = [call for row in rows for call in row["model_calls"]]
    durations = [row["duration_ms"] for row in rows]
    return {
        "schema_version": "matchos.quality.v1",
        "report_id": report["run_id"],
        "created_at": report["created_at"],
        "mode": report["mode"],
        "dataset_version": report["dataset_version"],
        "dataset_sha256": report.get("dataset_sha256"),
        "pipeline_fingerprint": report.get("pipeline_fingerprint"),
        "seed": report["seed"],
        "sample_size": len(rows),
        "authored_conditions": len({row["case_id"] for row in rows}),
        "note": "Authored synthetic regression cases, not independent real-match samples or general factual accuracy. Held rows do not count as evaluated prose.",
        "metrics": {
            "all_checks": ratio(sum(row["passed"] for row in rows), len(rows)),
            "numerical_fidelity": ratio(
                sum(row["checks"].get("numbers_supported") is True for row in aired), len(aired)
            ),
            "repeat_case_restraint": ratio(
                sum(row["publication"] == "HOLD" for row in repeats), len(repeats)
            ),
            "expired_case_restraint": ratio(
                sum(row["publication"] == "HOLD" for row in expired), len(expired)
            ),
            "quiet_case_restraint": ratio(
                sum(
                    row["publication"] == "HOLD" and row["checks"].get("unnecessary_model_calls") is True
                    for row in quiet
                ),
                len(quiet),
            ),
            "fresh_publications": ratio(
                sum(row["checks"].get("fresh_at_publication") is True for row in aired), len(aired)
            ),
            "fallback_rows": ratio(sum(row["fallback"] for row in rows), len(rows)),
            "timeout_rows": ratio(
                sum(
                    any(
                        call.get("error")
                        in {"TimeoutError", "ReadTimeout", "ConnectTimeout", "APITimeoutError"}
                        for call in row["model_calls"]
                    )
                    for row in rows
                ),
                len(rows),
            ),
        },
        "latency": {
            "unit": "ms",
            "samples": len(durations),
            "p50": percentile(durations, 0.5),
            "p95": percentile(durations, 0.95),
            "maximum": max(durations, default=None),
            "note": "Wall-clock viewer evaluation including holds; excludes checkpoint reconstruction.",
        },
        "models": sorted({call["model"] for call in calls}),
        "model_calls": len(calls),
        "cost": {
            "amount": None,
            "note": "No cost estimate: complete provider usage and deployment billing rates are not recorded. See Azure Cost Management for actual charges.",
        },
        "cases": [
            {
                "case_id": row["case_id"],
                "persona": row["persona"],
                "selected_frame": row["selected_frame"],
                "publication": row["publication"],
                "passed": row["passed"],
                "duration_ms": row["duration_ms"],
                "fallback": row["fallback"],
                "checks": row["checks"],
            }
            for row in rows
        ],
        "foundry_grading": report.get("foundry_grading"),
    }
