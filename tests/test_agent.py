import json

import pytest

from review_agent.agent import ReviewAgent
from review_agent.llm import ModelError
from review_agent.models import (
    AuditBatch,
    AuditDecision,
    Draft,
    Finding,
    Metrics,
    ReviewInput,
    normalize_draft,
)


def finding(line=1):
    return Finding(
        title="数值除零",
        severity="high",
        category="Bug",
        line_start=line,
        line_end=line,
        explanation="运行到该表达式时触发除零。",
        evidence="1 / 0",
        suggestion="修正除数。",
    )


def draft_response(findings=None):
    draft = Draft(summary="审查完成", findings=findings or [], limitations=[])
    return {"role": "assistant", "content": draft.model_dump_json()}


def audit_response(verdict="supported", **updates):
    values = dict(
        finding_id=1,
        verdict=verdict,
        reason="执行到常量 1 / 0 会抛出 ZeroDivisionError，没有保护分支。",
        basis="local_code",
        impact_kind="runtime_failure",
        severity="high",
    )
    values.update(updates)
    batch = AuditBatch(decisions=[AuditDecision(**values)])
    return {"role": "assistant", "content": batch.model_dump_json()}


def tool_response(name="run_static_checks", arguments="{}"):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": name, "arguments": arguments},
            },
        ],
    }


class ScriptedClient:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.messages = []
        self.metrics = Metrics()

    def complete(self, messages, **kwargs):
        self.messages.append([dict(message) for message in messages])
        self.metrics.model_calls += 1
        response = next(self.responses)
        if isinstance(response, Exception):
            raise response
        return response


def followup_response(answer="除数为零。", **updates):
    from review_agent.followup import FollowupDraft

    draft = FollowupDraft(
        answer=answer, validation_steps=["传入普通数值，核对预期异常；尚未运行。"], **updates
    )
    return {"role": "assistant", "content": draft.model_dump_json()}


def test_demo_end_to_end_and_zero_network_calls():
    report = ReviewAgent().review(ReviewInput(code="def f(x=[]):\n    return 1 / 0\n"))
    assert report.mode == "demo"
    assert report.reflection_status == "local_only"
    assert report.metrics.model_calls == 0
    assert report.metrics.tool_calls == 3
    assert {f.rule_id for f in report.findings} >= {"AST002", "AST005"}


def test_real_loop_returns_tool_result_then_reflects():
    client = ScriptedClient(
        [
            tool_response("read_source"),
            draft_response([finding()]),
            audit_response("rejected", reason="此测试复核器决定删除该候选。"),
        ]
    )
    report = ReviewAgent(client).review(ReviewInput(code="value = 1 / 0\n"))
    assert report.reflection_status == "completed"
    assert report.findings == []  # Reflection is actually used, not merely logged.
    result = next(message for message in client.messages[1] if message["role"] == "tool")
    assert result["tool_call_id"] == "call_1"
    assert "source_reference" in result["content"]
    assert "value = 1 / 0" not in result["content"]
    assert report.metrics.model_calls == 3


def test_reflection_failure_quarantines_initial_draft():
    client = ScriptedClient([draft_response([finding()]), ModelError("down")])
    report = ReviewAgent(client).review(ReviewInput(code="value = 1 / 0\n"))
    assert report.findings == []
    assert len(report.pending_findings) == 1
    assert report.pending_findings[0].severity == "medium"
    assert report.pending_findings[0].verification == "pending"
    assert report.reflection_status == "failed"
    assert any("复核失败" in item for item in report.limitations)


def test_draft_json_repair_is_bounded():
    client = ScriptedClient(
        [{"role": "assistant", "content": "bad json"}, draft_response([finding()])]
    )
    report = ReviewAgent(client).review(ReviewInput(code="x = 1 / 0\n"), "direct")
    assert report.findings == []
    assert len(report.pending_findings) == 1
    assert len(client.messages) == 2
    bad = ScriptedClient([{"content": "[]"}, {"content": "[]"}])
    with pytest.raises(ModelError, match="连续两次"):
        ReviewAgent(bad).review(ReviewInput(code="x = 1\n"), "direct")


def test_tool_loop_has_finite_rounds():
    client = ScriptedClient([tool_response("read_source")] * 4 + [draft_response()])
    report = ReviewAgent(client).review(ReviewInput(code="x = 1\n"), "tools")
    assert report.metrics.tool_calls == 4
    assert report.metrics.model_calls == 5
    assert any("轮次上限" in note for note in report.limitations)


def test_tool_batch_budget():
    response = tool_response("read_source")
    response["tool_calls"] = [
        dict(response["tool_calls"][0], id=f"call_{index}") for index in range(12)
    ]
    client = ScriptedClient([response, draft_response()])
    report = ReviewAgent(client).review(ReviewInput(code="x = 1\n"), "tools")
    assert report.metrics.tool_calls == 8
    assert any("调用上限" in note for note in report.limitations)


def test_bad_tool_arguments_are_returned_as_feedback():
    client = ScriptedClient([tool_response("read_source", '{"path":".env"}'), draft_response()])
    report = ReviewAgent(client).review(ReviewInput(code="x = 1\n"), "tools")
    tool_result = next(message for message in client.messages[1] if message["role"] == "tool")
    assert "error" in json.loads(tool_result["content"])
    assert any("未完成 run_static_checks" in note for note in report.limitations)


def test_normalize_invalid_lines_and_duplicates():
    draft = normalize_draft(
        Draft(summary="test", findings=[finding(), finding(), finding(99)]),
        ReviewInput(code="x = 1 / 0\n"),
    )
    assert len(draft.findings) == 1
    assert any("位置无效" in note for note in draft.limitations)


def test_followup_uses_snapshot_and_bounded_history():
    request = ReviewInput(code="x = 1 / 0\n")
    report = ReviewAgent().review(request)
    client = ScriptedClient([followup_response()])
    history = [
        {"role": "user" if i % 2 == 0 else "assistant", "content": str(i)} for i in range(12)
    ]
    assert "除数为零。" in ReviewAgent(client).follow_up(request, report, "为什么？", history)
    assert len(client.messages[0]) == 3
    assert len(json.loads(client.messages[0][1]["content"])["history"]) == 8
    assert "1: x = 1 / 0" == json.loads(client.messages[0][1]["content"])["numbered_source"]
    with pytest.raises(ValueError, match="代码已更改"):
        ReviewAgent(client).follow_up(ReviewInput(code="x = 2\n"), report, "为什么？")
