import json

from test_agent import ScriptedClient, draft_response, tool_response

from record_demo import RecordingClient, save_record


def test_recorder_keeps_public_tool_protocol_and_omits_hidden_reasoning():
    response = {
        **tool_response(),
        "reasoning_content": "private",
        "headers": {"Authorization": "secret"},
    }
    underlying = ScriptedClient([response])
    recording = RecordingClient(underlying)
    original = recording.complete(
        [
            {"role": "system", "content": "instructions"},
            {"role": "tool", "tool_call_id": "prior", "content": '{"status":"completed"}'},
        ]
    )
    assert original is response
    assert recording.metrics is underlying.metrics
    assert recording.calls[0]["response"] == tool_response()
    assert recording.calls[0]["tool_feedback"][0]["tool_call_id"] == "prior"
    assert "private" not in json.dumps(recording.calls)
    assert "Authorization" not in json.dumps(recording.calls)


def test_record_serialization_has_final_credential_guard(tmp_path):
    secret = "test-credential-value"
    output = tmp_path / "live.json"
    save_record(output, {"error": secret, "protocol": [draft_response()]}, secret)
    text = output.read_text(encoding="utf-8")
    assert secret not in text and "[REDACTED]" in text
