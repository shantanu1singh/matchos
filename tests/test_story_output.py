from test_evaluation import fixture

from matchos.story_output import StoryOutput


def test_published_contract_deduplication_and_viewer_specific_recap():
    _case, viewer, batch, result = fixture()
    output = StoryOutput()
    output.configure([viewer])
    packet = output.publish(result, viewer, 0, batch.state.timestamp_ms)
    assert packet["schema_version"] == "matchos.insight.v1"
    assert packet["evidence"]["facts"] == result.moment.facts
    assert packet["clock"]["expires_at_ms"] == result.moment.expires_at_ms
    assert output.publish(result, viewer, 0, batch.state.timestamp_ms) is None
    recap = output.snapshot(batch.state.timestamp_ms)["views"][0]["recap"]
    assert recap["items"][0]["body"] == result.overlay.body
    assert recap["source"] == "aired-story-extract"
    assert not output.snapshot(batch.state.timestamp_ms - 1)["views"][0]["recap"]["items"]
    other = viewer.model_copy(update={"language": "es"})
    output.configure([other])
    assert output.snapshot(batch.state.timestamp_ms)["views"][0]["stories"] == []


def test_holds_and_expired_results_never_enter_output_and_demo_reset_controls_history():
    _case, viewer, batch, result = fixture()
    output = StoryOutput()
    output.configure([viewer])
    held = result.model_copy(deep=True)
    held.producer.action = "SUPPRESS"
    assert output.publish(held, viewer, 0, batch.state.timestamp_ms) is None
    assert output.publish(result, viewer, 0, result.moment.expires_at_ms) is None
    assert output.publish(result, viewer, 0, batch.state.timestamp_ms)
    previous_stream = output.stream_id
    output.reset(retain=True)
    assert output.stream_id != previous_stream and output.history[0]
    output.reset()
    assert output.history == {} and output.profiles == {}
