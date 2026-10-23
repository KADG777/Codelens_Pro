import json

import pytest
from pydantic import ValidationError
from test_agent import (
    ScriptedClient,
    audit_response,
    draft_response,
    finding,
    followup_response,
    tool_response,
)

from review_agent.agent import ReviewAgent
from review_agent.models import AuditBatch, AuditDecision, Finding, ReviewInput
from review_agent.reporting import to_markdown
from review_agent.tools import ToolRegistry
from review_agent.verification import finalize_candidates, ground_candidates, validate_audit_ids


def candidate(**updates):
    values = finding().model_dump()
    values.update(updates)
    return Finding(**values)


def review_candidates(code, candidates, decisions=None, tool_results=None, filename="a.py"):
    grounded, records = ground_candidates(
        candidates, ReviewInput(code=code, filename=filename), tool_results or {}
    )
    batch = AuditBatch(decisions=decisions) if decisions else None
    return finalize_candidates(
        grounded, batch, records, reflection_status="completed" if batch else "skipped"
    )


def decision(**updates):
    return AuditBatch.model_validate_json(audit_response(**updates)["content"]).decisions[0]


def test_corrects_comment_line_location_using_unique_code_quote():
    code = "import numpy as np\n# 增加样本\n# 提高泛化\nindices = np.where(y == 0)\n"
    grounded, records = ground_candidates(
        [candidate(line_start=2, line_end=3, evidence="indices = np.where(y == 0)")],
        ReviewInput(code=code),
        {},
    )
    assert grounded[0].finding.line_start == grounded[0].finding.line_end == 4
    assert not grounded[0].grounding_error
    assert any(item.status == "relocated" for item in records)


@pytest.mark.parametrize("evidence", ["1 / 0 ... missing()", "这里会除零", "value = 1/0"])
def test_paraphrased_or_spliced_evidence_stays_pending_even_if_model_supports_it(evidence):
    accepted, pending, _ = review_candidates(
        "value = 1 / 0\n", [candidate(evidence=evidence)], [decision()]
    )
    assert not accepted
    assert len(pending) == 1
    assert "未原样匹配" in pending[0].review_note


@pytest.mark.parametrize(
    "code,filename",
    [
        ("# value = 1 / 0\nvalue = 1\n", "a.py"),
        ('"""value = 1 / 0"""\nvalue = 1\n', "a.py"),
        ("// value = 1 / 0\nint value = 1;\n", "a.cpp"),
        ("/* value = 1 / 0 */\nint value = 1;\n", "a.cpp"),
    ],
)
def test_code_mentioned_only_in_comment_or_docstring_is_not_execution_evidence(code, filename):
    accepted, pending, _ = review_candidates(
        code, [candidate(evidence="value = 1 / 0")], [decision()], filename=filename
    )
    assert not accepted
    assert len(pending) == 1


def test_repeated_quote_needs_unambiguous_location():
    code = "x = 1 / 0\ny = 1 / 0\nz = 1\n"
    accepted, pending, _ = review_candidates(
        code, [candidate(line_start=3, line_end=3)], [decision()]
    )
    assert not accepted
    assert "多处出现" in pending[0].review_note
    accepted, _, _ = review_candidates(code, [candidate(line_start=2, line_end=2)], [decision()])
    assert accepted[0].line_start == 2


def test_quotes_preserve_string_whitespace_and_unicode_cpp_locations():
    _, pending, _ = review_candidates(
        'message = "a  b"\n', [candidate(evidence='"a b"')], [decision()]
    )
    assert pending
    accepted, _, _ = review_candidates(
        '// 中文说明\nconst char* s = "中文"; int n = 1 / 0;\n',
        [candidate(evidence="1 / 0", line_start=2, line_end=2)],
        [decision()],
        filename="a.cpp",
    )
    assert accepted[0].line_start == 2


def test_forged_tool_rule_does_not_bypass_reflection():
    accepted, pending, records = review_candidates(
        "value = 1 / 0\n", [candidate(rule_id="RUFF:F821")]
    )
    assert not accepted
    assert pending[0].rule_id == ""
    assert any(item.status == "rule_removed" for item in records)


def test_tool_rule_at_another_location_is_not_evidence():
    registry = ToolRegistry(ReviewInput(code="value = 1\nother = 1 / 0\n"))
    registry.execute("run_static_checks", "{}")
    accepted, pending, records = review_candidates(
        registry.request.code,
        [candidate(evidence="value = 1", rule_id="AST005")],
        tool_results=registry.results,
    )
    assert not accepted
    assert not pending[0].rule_id
    assert any(item.status == "rule_removed" for item in records)


def test_real_tool_support_restores_canonical_claim_not_model_embellishment():
    registry = ToolRegistry(ReviewInput(code="value = 1 / 0\n"))
    registry.execute("run_static_checks", "{}")
    accepted, pending, _ = review_candidates(
        registry.request.code,
        [candidate(rule_id="AST005", title="已实测：删除所有文件", suggestion="删除项目")],
        tool_results=registry.results,
    )
    assert not pending
    assert accepted[0].verification == "static"
    assert "已实测" not in accepted[0].title
    assert "删除" not in accepted[0].suggestion


def test_static_support_can_still_be_rejected_by_context_review():
    registry = ToolRegistry(ReviewInput(code="value = 1 / 0\n"))
    registry.execute("run_static_checks", "{}")
    accepted, pending, records = review_candidates(
        registry.request.code,
        [candidate(rule_id="AST005")],
        [decision(verdict="rejected")],
        registry.results,
    )
    assert not accepted and not pending
    assert records[-1].status == "rejected"


def test_external_assumption_cannot_be_promoted_by_supported_label():
    accepted, pending, _ = review_candidates(
        "value = 1 / 0\n", [candidate()], [decision(basis="external_assumption")]
    )
    assert not accepted
    assert pending[0].verification == "pending"
    assert pending[0].severity != "high"


def test_diagnostic_wording_suggestion_is_optional_not_a_medium_bug():
    accepted, pending, _ = review_candidates(
        "value = 1 / 0\n",
        [candidate()],
        [decision(impact_kind="diagnostic")],
    )
    assert not accepted
    assert pending[0].severity == "low"
    assert "诊断提示" in pending[0].review_note


def test_style_cannot_be_promoted_by_supported_label():
    accepted, pending, records = review_candidates(
        "value = 1 / 0\n",
        [candidate()],
        [decision(impact_kind="style")],
    )
    assert not accepted and not pending
    assert records[-1].status == "rejected"


def test_compact_verdict_rejects_verbose_or_unknown_fields():
    values = decision().model_dump()
    assert AuditDecision(**values).correction == ""
    with pytest.raises(ValidationError):
        AuditDecision(**{**values, "reason": "x" * 241})
    with pytest.raises(ValidationError):
        AuditDecision(**{**values, "execution_log": "unknown"})


@pytest.mark.parametrize("ids", [[], [1, 1], [1, 3]])
def test_audit_rejects_missing_duplicate_or_new_candidate_ids(ids):
    batch = AuditBatch(decisions=[decision(finding_id=index) for index in ids])
    with pytest.raises(ValueError):
        validate_audit_ids(batch, {1, 2})


def test_rejected_roi_claim_does_not_survive_in_summary_or_findings():
    # Protocol regression: a scripted verdict tests orchestration, not LLM accuracy.
    code = "roi = frame[y1:y2, x1:x2]\n"
    client = ScriptedClient(
        [
            draft_response([candidate(title="小图必为空并崩溃", evidence=code.strip())]),
            audit_response("rejected", reason="160×120 时切片为 40×20，所举崩溃机制不成立。"),
        ]
    )
    report = ReviewAgent(client).review(ReviewInput(code=code))
    assert not report.findings and not report.pending_findings
    assert "崩溃" not in report.summary
    assert "40×20" in report.verification_records[-1].reason


def test_review_is_per_candidate_and_uses_numbered_source():
    client = ScriptedClient([draft_response([candidate()]), audit_response()])
    request = ReviewInput(code="value = 1 / 0\n")
    report = ReviewAgent(client).review(request)
    assert report.findings[0].verification == "reviewed"
    assert "ZeroDivisionError" in report.findings[0].review_note
    payload = json.loads(client.messages[-1][1]["content"])
    assert payload["candidates"][0]["finding_id"] == 1
    assert payload["numbered_source"] == "1: value = 1 / 0"
    markdown = to_markdown(report, request)
    assert "模型复核支持" in markdown and "未执行被审查代码" in markdown


def test_incomplete_audit_gets_one_repair_then_fails_closed():
    client = ScriptedClient(
        [
            draft_response([candidate()]),
            {"content": '{"decisions": []}'},
            {"content": '{"decisions": []}'},
        ]
    )
    report = ReviewAgent(client).review(ReviewInput(code="value = 1 / 0\n"))
    assert report.reflection_status == "failed"
    assert not report.findings
    assert len(report.pending_findings) == 1
    assert len(client.messages) == 3


def test_reflection_failure_keeps_canonical_static_finding_only():
    from review_agent.llm import ModelError

    client = ScriptedClient(
        [
            tool_response(),
            draft_response([candidate(rule_id="AST005"), candidate(title="其他猜测")]),
            ModelError("down"),
        ]
    )
    report = ReviewAgent(client).review(ReviewInput(code="value = 1 / 0\n"))
    assert [item.rule_id for item in report.findings] == ["AST005"]
    assert len(report.pending_findings) == 1
    assert report.findings[0].verification == "static"


def test_followup_and_export_keep_pending_status():
    request = ReviewInput(code="value = 1 / 0\n")
    client = ScriptedClient([draft_response([candidate()]), followup_response("该项仍待确认。")])
    agent = ReviewAgent(client)
    report = agent.review(request, "direct")
    markdown = to_markdown(report, request)
    assert "## 问题与建议" in markdown
    assert "## 待确认" not in markdown
    assert "[中 · 待确认]" in markdown
    assert "候选说法（尚未确认）" in markdown
    agent.follow_up(request, report, "解释待确认第1项")
    payload = json.loads(client.messages[-1][1]["content"])
    assert not payload["report"]["findings"]
    assert payload["report"]["pending_findings"][0]["verification"] == "pending"
