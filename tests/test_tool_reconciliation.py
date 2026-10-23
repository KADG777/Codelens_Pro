"""Pipeline regressions: actual tool findings survive an incomplete model draft."""

import json

import pytest
from test_agent import ScriptedClient, audit_response, draft_response, finding, tool_response

from review_agent.agent import ReviewAgent
from review_agent.llm import ModelError
from review_agent.models import ReviewInput
from review_agent.reporting import to_markdown
from review_agent.tools import ToolRegistry
from review_agent.verification import reconcile_candidates, static_findings


@pytest.mark.parametrize("strategy", ["tools", "full"])
def test_model_empty_draft_does_not_erase_real_static_finding(strategy):
    responses = [tool_response(), draft_response()]
    if strategy == "full":
        responses.append(audit_response())
    client = ScriptedClient(responses)
    request = ReviewInput(code="value = 1 / 0\n")
    report = ReviewAgent(client).review(request, strategy)
    assert len(report.findings) == 1
    assert report.findings[0].rule_id == "AST005"
    assert report.findings[0].verification == "static"
    assert any(item.status == "recovered" for item in report.verification_records)
    assert any("补回" in event.detail for event in report.events)
    assert client.metrics.model_calls == (3 if strategy == "full" else 2)


def test_already_reported_rule_is_not_duplicated_or_marked_recovered():
    item = finding().model_copy(update={"rule_id": "AST005"})
    client = ScriptedClient([tool_response(), draft_response([item]), audit_response()])
    report = ReviewAgent(client).review(ReviewInput(code="value = 1 / 0\n"))
    assert len(report.findings) == 1
    assert not any(record.status == "recovered" for record in report.verification_records)


def test_relocated_rule_is_not_duplicated_by_reconciliation():
    item = finding().model_copy(update={"rule_id": "AST005"})
    request = ReviewInput(code="value = 1\nother = 1 / 0\n")
    client = ScriptedClient([tool_response(), draft_response([item]), audit_response()])
    report = ReviewAgent(client).review(request)
    assert len(report.findings) == 1 and report.findings[0].line_start == 2
    assert any(record.status == "duplicate" for record in report.verification_records)


def test_recovered_static_finding_can_still_be_rejected_by_context_audit():
    client = ScriptedClient(
        [
            tool_response(),
            draft_response(),
            audit_response("rejected", reason="模拟上下文复核否决；验证不会在裁决后重新补回。"),
        ]
    )
    report = ReviewAgent(client).review(ReviewInput(code="value = 1 / 0\n"))
    assert not report.findings and not report.pending_findings
    assert {item.status for item in report.verification_records} >= {"recovered", "rejected"}


def test_failed_audit_preserves_actual_tool_result_and_failure_label():
    client = ScriptedClient([tool_response(), draft_response(), ModelError("unavailable")])
    report = ReviewAgent(client).review(ReviewInput(code="value = 1 / 0\n"))
    assert report.reflection_status == "failed"
    assert report.findings[0].verification == "static"
    assert report.findings[0].rule_id == "AST005"


def test_no_static_tool_call_means_no_invented_static_support():
    report = ReviewAgent(ScriptedClient([draft_response()])).review(
        ReviewInput(code="value = 1 / 0\n")
    )
    assert not report.findings
    assert any("未完成 run_static_checks" in warning for warning in report.limitations)
    assert not any(record.status == "recovered" for record in report.verification_records)


def test_low_priority_real_ruff_finding_is_recovered_without_extra_audit():
    client = ScriptedClient([tool_response(), draft_response()])
    report = ReviewAgent(client).review(
        ReviewInput(code="def label():\n    conf = 1\n    return 0\n")
    )
    assert len(report.findings) == 1
    assert report.findings[0].rule_id == "RUFF:F841"
    assert report.findings[0].line_start == 2
    assert client.metrics.model_calls == 2


def test_cpp_tool_result_is_also_recovered():
    request = ReviewInput(
        code="void release() {\n    int* p = new int[4];\n    delete p;\n}\n", filename="a.cpp"
    )
    client = ScriptedClient([tool_response(), draft_response()])
    report = ReviewAgent(client).review(request, "tools")
    assert len(report.findings) == 1
    assert report.findings[0].verification == "static"
    assert report.findings[0].line_start == 3
    assert report.findings[0].rule_id.startswith("CPP")


def test_direct_baseline_remains_tool_free():
    client = ScriptedClient([draft_response()])
    report = ReviewAgent(client).review(ReviewInput(code="value = 1 / 0\n"), "direct")
    assert not report.findings and report.metrics.tool_calls == 0


def test_report_limit_is_after_merge_and_keeps_late_high_severity():
    code = (
        "".join(f"def f{i}(items=[]):\n    return items\n" for i in range(30)) + "value = 1 / 0\n"
    )
    request = ReviewInput(code=code)
    report = ReviewAgent().review(request)
    assert len(report.findings) == 30
    assert report.findings[0].rule_id == "AST005" and report.findings[0].line_start == 61
    assert any("共 31 条" in warning and "省略 1 条" in warning for warning in report.limitations)
    assert sum(record.status == "truncated" for record in report.verification_records) == 1
    assert "报告候选已截断" in to_markdown(report, request)


def test_candidate_ids_stay_valid_after_priority_sort_and_full_budget():
    request = ReviewInput(code="def f(items=[]):\n    return items\nvalue = 1 / 0\n")
    registry = ToolRegistry(request)
    registry.execute("run_static_checks", "{}")
    candidates, records, warnings = reconcile_candidates([], request, registry.results, limit=1)
    assert len(candidates) == 1 and candidates[0].finding.rule_id == "AST005"
    assert candidates[0].finding_id in {1, 2}
    assert any(record.status == "truncated" for record in records)
    assert any("共 2 条" in warning for warning in warnings)
    assert len(static_findings(registry.results)) == 2


def test_recovered_ids_are_included_in_actual_audit_payload():
    client = ScriptedClient([tool_response(), draft_response(), audit_response()])
    ReviewAgent(client).review(ReviewInput(code="value = 1 / 0\n"))
    payload = json.loads(client.messages[-1][1]["content"])
    assert [(item["finding_id"], item["actual_static"]) for item in payload["candidates"]] == [
        (1, True)
    ]


@pytest.mark.parametrize(
    "code,rule,expected",
    [
        ("x = 1 / 0; y = 2 / 0\n", "AST005", 2),
        ("print(missing_a, missing_b)\n", "RUFF:F821", 2),
    ],
)
def test_distinct_diagnostics_on_same_line_are_preserved(code, rule, expected):
    report = ReviewAgent().review(ReviewInput(code=code))
    matches = [item for item in report.findings if item.rule_id == rule]
    assert len(matches) == expected
    assert all(item.verification == "static" for item in matches)


def test_reporting_one_diagnostic_does_not_cover_other_diagnostics_on_same_line():
    request = ReviewInput(code="x = 1 / 0; y = 2 / 0\n")
    registry = ToolRegistry(request)
    registry.execute("run_static_checks", "{}")
    first = static_findings(registry.results)[0]
    candidates, records, _ = reconcile_candidates([first], request, registry.results)
    assert len(candidates) == 2 and all(item.static for item in candidates)
    assert sum(item.status == "recovered" for item in records) == 1


def test_multiline_quote_with_unique_real_rule_anchor_restores_canonical_result():
    request = ReviewInput(code="def broken():\n    return 10 / 0\n")
    registry = ToolRegistry(request)
    registry.execute("run_static_checks", "{}")
    claim = finding().model_copy(
        update={
            "line_start": 1,
            "line_end": 2,
            "evidence": request.code.strip(),
            "rule_id": "AST005",
        }
    )
    candidates, records, _ = reconcile_candidates([claim], request, registry.results)
    assert len(candidates) == 1 and candidates[0].static
    assert candidates[0].finding.line_start == 2
    assert not any(item.status == "rule_removed" for item in records)


def test_multiline_quote_cannot_choose_between_two_same_rule_diagnostics():
    request = ReviewInput(code="def broken():\n    x = 1 / 0\n    return 2 / 0\n")
    registry = ToolRegistry(request)
    registry.execute("run_static_checks", "{}")
    claim = finding().model_copy(
        update={
            "line_start": 1,
            "line_end": 3,
            "evidence": request.code.strip(),
            "rule_id": "AST005",
        }
    )
    candidates, _, _ = reconcile_candidates([claim], request, registry.results)
    assert not next(c for c in candidates if c.finding_id == 1).static
    assert sum(c.static and c.finding.rule_id == "AST005" for c in candidates) == 2
