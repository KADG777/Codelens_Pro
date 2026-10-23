"""Exercise internal repair context and the separation of past/current failures."""

import json

import pytest
from test_agent import ScriptedClient, followup_response

from review_agent.agent import ReviewAgent
from review_agent.llm import ModelError
from review_agent.models import ReviewInput

SOURCE = (
    "def process(raw):\n"
    "    prepared = raw + 1\n"
    "    prediction = prepared * 2\n"
    "    preview = prepared\n"
    "    return prediction, preview\n"
)


def proposal(start, end, replacement):
    return {
        "line_start": start, "line_end": end,
        "original": "\n".join(SOURCE.splitlines()[start - 1:end]),
        "replacement": replacement, "rationale": "统一处理结果与预览。",
    }


def candidates():
    bad = followup_response("建议去掉偏移。", patches=[
        proposal(2, 2, "    normalized = raw"),
    ])
    good = followup_response("推理和预览均使用 normalized，尚未验证效果。", patches=[
        proposal(2, 2, "    # Shared input\n    normalized = raw"),
        proposal(3, 4, "    prediction = normalized * 2\n    preview = normalized"),
    ])
    return bad, good


def run(responses):
    request = ReviewInput(code=SOURCE, filename="pipeline.py")
    report = ReviewAgent().review(request)
    client = ScriptedClient(responses)
    agent = ReviewAgent(client)
    answer = agent.follow_up(request, report, "修改代码，使推理与预览都去掉偏移")
    return agent.last_followup, client, request, answer


@pytest.mark.parametrize("verdict", ["supported", "revise"])
def test_repair_audit_receives_internal_history_and_fully_composed_source(verdict):
    bad, good = candidates()
    result, client, request, answer = run([
        bad, good,
        {"content": json.dumps({"verdict": verdict, "reason": "需要进一步说明效果评估。"})},
    ])
    audit = json.loads(client.messages[2][-1]["content"])
    assert audit["context"]["history"] == []
    previous = audit["repair_history"][0]
    assert previous["not_applied"] is True and previous["failed_stage"] == "local"
    assert "F821" in previous["errors"][0]
    assert previous["candidate"]["patches"][0]["original"] == "    prepared = raw + 1"
    assert audit["context"]["source_basis"] == "proposed_after_all_patches_not_executed"
    assert audit["context"]["report_and_facts_basis"] == "original_snapshot_before_patches"
    composed = audit["context"]["numbered_source"]
    assert "2:     # Shared input\n3:     normalized = raw" in composed
    assert "4:     prediction = normalized * 2\n5:     preview = normalized" in composed
    assert composed.endswith("6:     return prediction, preview")
    assert "prepared" not in composed
    assert request.code == SOURCE
    assert result.metrics.model_calls == 3
    assert result.attempts[0].local_status == "failed"
    assert result.attempts[0].audit_status == "skipped"
    assert result.attempts[1].local_status == "passed"
    assert any("后续候选已通过本地校验" in check and "F821" in check for check in result.checks)
    assert "F821" not in answer
    if verdict == "supported":
        assert result.status == "repaired" and result.patch_count == 2
        assert "替换原文件" in answer
    else:
        assert result.status == "blocked" and result.patch_count == 0
        assert "需要进一步说明效果评估" in answer and "替换原文件" not in answer


def test_audit_rejection_is_carried_to_next_audit_without_becoming_user_history():
    _, good = candidates()
    result, client, _, _ = run([
        good, {"content": '{"verdict":"revise","reason":"应说明评估方法。"}'},
        good, {"content": '{"verdict":"supported","reason":"方案可行，效果待实验。"}'},
    ])
    first = json.loads(client.messages[1][-1]["content"])
    second = json.loads(client.messages[3][-1]["content"])
    assert first["repair_history"] == []
    assert second["repair_history"][0]["failed_stage"] == "audit"
    assert "应说明评估方法" in second["repair_history"][0]["errors"][0]
    assert second["context"]["history"] == []
    assert result.status == "repaired" and result.metrics.model_calls == 4


def test_final_generation_failure_does_not_repeat_old_patch_error_as_current():
    bad, _ = candidates()
    result, _, _, answer = run([bad, ModelError("offline")])
    assert result.status == "blocked" and result.metrics.model_calls == 2
    assert "服务未返回" in answer and "F821" not in answer
    assert result.attempts[-1].local_status == "skipped"
    assert any("历史候选" in check and "F821" in check for check in result.checks)


def test_repeated_invalid_patch_still_blocks_without_extra_audit_or_retry():
    bad, _ = candidates()
    result, _, _, answer = run([bad, bad])
    assert result.status == "blocked" and result.metrics.model_calls == 2
    assert "F821" in answer and "替换原文件" not in answer
    assert all(item.audit_status == "skipped" for item in result.attempts)


def test_malformed_first_response_can_repair_without_inventing_previous_patch():
    _, good = candidates()
    result, client, _, _ = run([
        {"content": "not json"}, good,
        {"content": '{"verdict":"supported","reason":"方案可行。"}'},
    ])
    audit = json.loads(client.messages[2][-1]["content"])
    assert audit["repair_history"][0]["candidate"] is None
    assert result.status == "repaired"
