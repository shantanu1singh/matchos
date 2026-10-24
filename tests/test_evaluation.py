"""Evaluation checks and Foundry wire requests; no paid inference or Azure access."""
import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from matchos.evaluation import (cloud_rows, evaluate_case, grading_criteria, load_cases, main,
                                score_result, submit_foundry, summarize, write_report)
from matchos.schemas import (Check, MomentDecision, PipelineResult, ProducerDecision,
                             VerifiedCandidates, VerifiedMoment, ViewerProfile)
from matchos.state_engine import StateEngine
from matchos.telemetry import Telemetry


def fixture():
    state = StateEngine().state
    state.timestamp_ms = 3840000
    moment = VerifiedMoment(id="press-1", type="TACTICAL_SHIFT", team_id="RED", frame="MOMENTUM_SWING",
        importance=.9, confidence=.9, facts={"leadingTeam": "Liverpool", "trailingTeam": "Manchester City",
        "possessionLast5": .64, "finalThirdEntriesBefore": 2, "finalThirdEntries": 13}, evidence=["pass-1"],
        evidence_detail=[], valid_from_ms=state.timestamp_ms, expires_at_ms=state.timestamp_ms+40000,
        checks=[Check(name="facts_bound", passed=True, detail="canonical state")])
    batch = PipelineResult(kind="candidates", state=state, candidates=VerifiedCandidates(candidates=[moment]))
    viewer = ViewerProfile(persona="analyst")
    case = {"id": "press", "mode": "fresh", "expected_frames": ["MOMENTUM_SWING"], "expected_publication": "AIR", "rubric": "New territorial change"}
    decision = MomentDecision(selected_candidate_id=moment.id, tactical_shift=.9, momentum_shift=.9,
        player_story=0, importance=4.5, primary_frame=moment.frame, primary_frame_confidence=.9,
        should_surface=.9, frame_scores={moment.frame:.9}, provider="test", latency_ms=1)
    from matchos.copy import render_overlay
    result = PipelineResult(kind="moment", moment=moment, decision=decision,
        producer=ProducerDecision(action="AIR", reason="new"), overlay=render_overlay(moment, viewer, rich=True, provider="test"))
    return case, viewer, batch, result


def test_report_fails_on_unsupported_numbers_invalid_selection_and_provider_errors():
    case, viewer, batch, result = fixture()
    good = score_result(case, viewer, batch, result, [], 20)
    assert good["passed"]
    result.overlay.body = "Liverpool have 999% possession."
    result.decision.selected_candidate_id = "invented-candidate"
    bad = score_result(case, viewer, batch, result, [], 40000, error="TimeoutError")
    for key in ("numbers_supported", "selection_in_verified_pool", "latency_budget", "execution_complete", "editorial_expectation"):
        assert bad["checks"][key] is False
    assert bad["selected_frame"] == "INVALID" and not bad["passed"]


def test_publishing_a_different_verified_candidate_than_the_models_choice_is_a_failure():
    case, viewer, batch, result = fixture()
    alternative=batch.candidates.candidates[0].model_copy(update={"id":"other-verified-story"})
    batch.candidates.candidates.append(alternative)
    result.decision.selected_candidate_id=alternative.id
    row=score_result(case,viewer,batch,result,[],20)
    assert row['checks']['selection_in_verified_pool'] is True
    assert row['checks']['published_selected_candidate'] is False
    assert not row['passed']


def test_hold_rows_do_not_inflate_copy_grounding_scores_and_reports_preserve_failures(tmp_path):
    case, viewer, batch, result = fixture()
    good = score_result(case, viewer, batch, result, [], 20)
    hold_case = {**case, "expected_frames": ["NONE"], "expected_publication": "HOLD"}
    held = score_result(hold_case, viewer, batch, PipelineResult(kind="trace", producer=ProducerDecision(action="SUPPRESS", reason="quiet")), [], 10)
    assert held["checks"]["numbers_supported"] is None
    summary = summarize([good, held])
    assert summary["checks"]["numbers_supported"] == {"passed":1,"evaluated":1,"rate":1}
    report = {"mode":"local-baseline", "seed":47129, "created_at":"test", "dataset_note":"authored expectations",
              "dataset_version":"1", "rows":[good,held], "summary":summary}
    output = tmp_path / "report.json"
    write_report(report, output)
    assert json.loads(output.read_text())["mode"] == "local-baseline"
    assert "Local checks do not grade editorial prose" in output.with_suffix('.md').read_text()
    assert len(cloud_rows(report)) == 2


def test_expired_case_runs_real_worker_without_calling_models():
    case, viewer, batch, _ = fixture()
    async def forbidden(*args):
        pytest.fail("Expired evidence must not call the decision model")
    session = SimpleNamespace(live_state_engine=SimpleNamespace(state=batch.state), telemetry=Telemetry(),
                              services=SimpleNamespace(decisions=SimpleNamespace(select=forbidden)))
    case.update(id="expired", mode="expired", expected_frames=["NONE"], expected_publication="HOLD")
    row = asyncio.run(evaluate_case(session, batch, case, viewer, 0))
    assert row["passed"] and row["model_calls"] == []
    assert row["checks"]["unnecessary_model_calls"] is True
    # Each evaluation gets a copy; the next case still sees fresh evidence.
    assert batch.candidates.candidates[0].expires_at_ms > batch.state.timestamp_ms


def test_repeatable_dataset_covers_three_personas_and_does_not_require_forced_disagreement():
    cases = load_cases()
    assert len(cases["cases"]) == 8
    assert {case["mode"] for case in cases["cases"]} == {"fresh","recent","expired","major-aired"}
    assert "not human-labelled" in cases["description"]
    editorial = next(item for item in grading_criteria("judge") if item["name"] == "editorial_selection")
    assert "never reward disagreement" in editorial["input"][0]["content"]


def test_foundry_submission_uses_saved_actual_outputs_and_project_evals_wire_contract():
    pytest.importorskip("azure.ai.projects")
    from openai import OpenAI
    case, viewer, batch, result = fixture()
    row = score_result(case, viewer, batch, result, [], 20)
    report = {"rows":[row], "mode":"configured-providers", "dataset_version":"1", "created_at":"test"}
    requests = []
    def respond(request):
        payload = json.loads(request.content)
        requests.append((request.url.path,payload))
        if request.url.path.endswith('/runs'):
            return httpx.Response(200,json={"id":"run_1","object":"eval.run","status":"queued", "report_url":"https://example.test/report"})
        return httpx.Response(200,json={"id":"eval_1","object":"eval","created_at":1,
            "data_source_config":payload['data_source_config'],"testing_criteria":payload['testing_criteria']})
    with OpenAI(api_key="fake",base_url="https://foundry.test/api/projects/matchos/openai/v1",
                http_client=httpx.Client(transport=httpx.MockTransport(respond))) as client:
        saved = submit_foundry(report,client,"judge-deployment")
    assert saved["eval_id"] == "eval_1" and saved["run_id"] == "run_1"
    assert saved["status"] == "queued"  # Submission is never claimed as a passing grade.
    assert requests[1][0].endswith('/evals/eval_1/runs')
    criteria = requests[0][1]["testing_criteria"]
    assert len([item for item in criteria if item['type']=='score_model']) == 4
    assert all(item['model']=='judge-deployment' for item in criteria if item['type']=='score_model')
    data = requests[1][1]['data_source']
    assert data['type']=='jsonl' and data['source']['type']=='file_content'
    actual = json.loads(data['source']['content'][0]['item']['response'])
    assert actual['overlay']['body'] == result.overlay.body
    assert actual['decision']['provider']=='test'


def test_missing_project_settings_are_actionable_and_do_not_attempt_auth(monkeypatch,capsys,tmp_path):
    monkeypatch.setattr('matchos.evaluation.load_env_files', lambda: None)
    monkeypatch.delenv('AZURE_AI_PROJECT_ENDPOINT',raising=False)
    assert main(['submit',str(tmp_path/'missing.json'),'--judge-model','judge'])==2
    assert '/api/projects/PROJECT' in capsys.readouterr().out


def test_completed_cloud_run_with_failing_rows_saves_real_grades_and_fails_exit_code(monkeypatch,tmp_path):
    from matchos import evaluation
    class Response:
        status='completed'
        report_url='https://foundry.test/report'
        def model_dump(self, **kwargs):
            return {'status':'completed','result_counts':{'passed':2,'failed':1,'errored':0}}
    class Item:
        def model_dump(self, **kwargs):
            return {'id':'row-1','results':[{'score':.2,'passed':False,'reason':'Repeated story'}]}
    runs = SimpleNamespace(retrieve=lambda *args,**kwargs:Response(),
                           output_items=SimpleNamespace(list=lambda *args,**kwargs:[Item()]))
    monkeypatch.setattr(evaluation,'project_client',lambda stack:SimpleNamespace(evals=SimpleNamespace(runs=runs)))
    path=tmp_path/'run.foundry.json'
    path.write_text(json.dumps({'eval_id':'eval_1','run_id':'run_1','status':'queued'}))
    assert main(['status',str(path)])==1
    saved=json.loads(path.read_text())
    assert saved['status']=='completed' and saved['report_url']=='https://foundry.test/report'
    assert saved['output_items'][0]['results'][0]['reason']=='Repeated story'
