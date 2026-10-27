import asyncio
import json

import pytest
from fastapi.testclient import TestClient
from test_evaluation import fixture

from matchos.evaluation import score_result
from matchos.quality import scorecard
from matchos.report_store import ReportStore
from matchos.server import app


def report():
    case, viewer, batch, result = fixture()
    row = score_result(case, viewer, batch, result, [], 1200)
    return {
        "run_id": "20261026T010101-test",
        "created_at": "2026-10-26T01:01:01Z",
        "mode": "local-baseline",
        "dataset_version": "1",
        "seed": 47129,
        "rows": [row],
    }


def test_scorecard_denominators_do_not_reward_missing_prose_or_unmeasured_restraint():
    data = report()
    card = scorecard(data)
    assert card["metrics"]["numerical_fidelity"] == {"passed": 1, "evaluated": 1, "rate": 1}
    assert card["metrics"]["quiet_case_restraint"]["rate"] is None
    assert card["latency"]["p50"] == 1200 and card["latency"]["p95"] == 1200
    assert card["cost"]["amount"] is None
    assert "input" not in card["cases"][0] and "output" not in card["cases"][0]
    data["rows"][0]["publication"] = "HOLD"
    assert scorecard(data)["metrics"]["numerical_fidelity"]["rate"] is None


def test_archive_routes_are_read_only_and_report_ids_cannot_escape_directory(monkeypatch, tmp_path):
    monkeypatch.setenv("MATCHOS_REPORTS_DIR", str(tmp_path))
    monkeypatch.setenv("MATCHOS_REPORTS_CONTAINER_URL", "")
    card = scorecard(report())
    asyncio.run(ReportStore(tmp_path).save(card))
    with TestClient(app) as client:
        listed = client.get("/api/evaluations").json()
        assert listed["storage"] == "local-json" and listed["reports"][0]["report_id"] == card["report_id"]
        assert client.get("/api/evaluations/" + card["report_id"]).json() == card
        assert client.get("/api/evaluations/invalid.name").status_code == 404
        assert client.post("/api/evaluations", json=card).status_code == 405
        assert client.get("/broadcast").status_code == 200
    assert json.loads((tmp_path / (card["report_id"] + ".json")).read_text()) == card


def test_blob_configuration_never_accepts_credentials_or_arbitrary_destinations():
    for url in (
        "http://example.com/reports",
        "https://evil.example/reports",
        "https://account.blob.core.windows.net/evaluations?sig=secret",
    ):
        with pytest.raises(ValueError):
            ReportStore(container_url=url)


def test_expiration_check_uses_actual_publication_clock():
    case, viewer, batch, result = fixture()
    result.published_at_ms = result.moment.expires_at_ms + 1
    row = score_result(case, viewer, batch, result, [], 100)
    assert row["checks"]["fresh_at_publication"] is False


def test_archive_lists_latest_publication_per_run_without_merging_distinct_executions(tmp_path):
    async def run():
        store = ReportStore(tmp_path, container_url="")
        original = scorecard(report())
        await store.save(original)
        published = {**original, "report_id": "20261026T020000-copy", "source_run_id": original["report_id"]}
        await store.save(published)
        latest = {
            **published,
            "report_id": "20261026T030000-graded",
            "foundry_grading": {"status": "completed"},
        }
        await store.save(latest)
        # Identical timestamps/configuration can still belong to separate runs.
        other = {**original, "report_id": "20261026T010101-other"}
        await store.save(other)
        listed = await store.list()
        assert {item["report_id"] for item in listed["reports"]} == {latest["report_id"], other["report_id"]}
        assert (await store.get(latest["report_id"]))["foundry_grading"]["status"] == "completed"
        assert await store.get(original["report_id"]) == original
        assert await store.get(published["report_id"]) == published

    asyncio.run(run())
