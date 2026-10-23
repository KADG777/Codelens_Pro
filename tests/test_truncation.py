"""Regression coverage for late high-priority findings and explicit output caps."""

import json
from types import SimpleNamespace

import pytest

from review_agent.cpp_tools import cpp_checks
from review_agent.demo import DemoClient
from review_agent.models import Draft, Finding, ReviewInput
from review_agent.selection import select_findings
from review_agent.tools import ToolRegistry, compact_tool_result, ruff_checks


def make_finding(line, *, severity="low", rule="TEST001", evidence=None):
    return Finding(
        title="测试诊断",
        severity=severity,
        category="Bug",
        line_start=line,
        line_end=line,
        explanation="本地测试诊断",
        suggestion="检查相关表达式",
        evidence=evidence or f"statement_{line}",
        rule_id=rule,
    )


def assert_counts(result, total, retained, *, duplicates=0):
    assert result["total_count"] == total
    assert result["unique_count"] == total - duplicates
    assert result["retained_count"] == retained
    assert result["duplicate_count"] == duplicates
    assert result["omitted_count"] == total - duplicates - retained
    assert result["truncated"] is (total - duplicates > retained)


def test_selection_keeps_late_high_priority_deterministically_and_counts_duplicates():
    items = [make_finding(line) for line in range(1, 31)]
    high = make_finding(31, severity="high")
    inputs = [*items, high, items[0].model_copy(deep=True)]
    chosen, stats = select_findings(inputs, 30)
    backwards, reverse_stats = select_findings(reversed(inputs), 30)
    assert chosen == backwards
    assert stats == reverse_stats
    assert chosen[0] == high
    assert [item.line_start for item in chosen] == [31, *range(1, 30)]
    assert_counts(stats, 32, 30, duplicates=1)


def test_same_line_different_rules_or_evidence_are_not_lost():
    findings = [
        make_finding(1, rule="A", evidence="left / 0"),
        make_finding(1, rule="A", evidence="right / 0"),
        make_finding(1, rule="B", evidence="left / 0"),
    ]
    retained, stats = select_findings(findings, 40)
    assert len(retained) == 3
    assert_counts(stats, 3, 3)


@pytest.mark.parametrize("limit", [0, -1, True, 1.5])
def test_invalid_selection_budget_is_rejected(limit):
    with pytest.raises(ValueError, match="positive integer"):
        select_findings([], limit)


def test_selection_at_limit_is_not_truncated():
    retained, stats = select_findings([make_finding(i) for i in range(1, 41)], 40)
    assert len(retained) == 40
    assert_counts(stats, 40, 40)


def test_python_ast_keeps_late_division_after_more_than_forty_warnings(monkeypatch):
    monkeypatch.setattr(
        "review_agent.tools.ruff_checks", lambda code: {"status": "completed", "findings": []}
    )
    code = "\n".join(f"def f{i}(items=[]): pass" for i in range(40)) + "\nresult = 1 / 0\n"
    result = ToolRegistry(ReviewInput(code=code)).execute("run_static_checks", "{}")
    assert result["builtin_findings"][0]["rule_id"] == "AST005"
    assert result["builtin_findings"][0]["line_start"] == 41
    assert_counts(result, 41, 40)
    assert_counts(result["builtin_selection"], 41, 40)
    assert "保留 40 条" in result["truncation_notice"]
    assert "省略 1 条" in result["truncation_notice"]


def test_ruff_keeps_late_undefined_name_after_forty_low_diagnostics(monkeypatch):
    raw = [
        {"code": "F401", "message": "unused import", "location": {"row": i}} for i in range(1, 41)
    ] + [{"code": "F821", "message": "undefined name", "location": {"row": 41}}]
    monkeypatch.setattr(
        "review_agent.tools.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=json.dumps(raw)),
    )
    result = ruff_checks("\n".join(f"statement_{i}" for i in range(1, 42)))
    assert result["findings"][0]["rule_id"] == "RUFF:F821"
    assert result["findings"][0]["line_start"] == 41
    assert_counts(result, 41, 40)
    assert "Ruff" in result["truncation_notice"]
    assert "省略 1 条" in result["truncation_notice"]


def test_real_ruff_late_high_priority_diagnostic_is_preserved():
    code = "\n".join(f"import module_{i}" for i in range(40)) + "\nprint(missing_name)\n"
    result = ruff_checks(code)
    assert result["status"] == "completed"
    assert result["findings"][0]["rule_id"] == "RUFF:F821"
    assert result["findings"][0]["line_start"] == 41
    assert_counts(result, 41, 40)


def test_real_ruff_preserves_different_undefined_names_on_same_line():
    result = ruff_checks("print(missing_a, missing_b)\n")
    assert result["status"] == "completed"
    assert_counts(result, 2, 2)
    assert {item["explanation"] for item in result["findings"]} == {
        "Undefined name `missing_a`",
        "Undefined name `missing_b`",
    }
    assert {item["line_start"] for item in result["findings"]} == {1}
    assert {item["rule_id"] for item in result["findings"]} == {"RUFF:F821"}


def test_ruff_deduplicates_only_identical_diagnostics(monkeypatch):
    first = {"code": "F821", "message": "Undefined name `a`", "location": {"row": 1}}
    second = {"code": "F821", "message": "Undefined name `b`", "location": {"row": 1}}
    monkeypatch.setattr(
        "review_agent.tools.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1, stdout=json.dumps([first, second, first.copy()])
        ),
    )
    result = ruff_checks("print(a, b)\n")
    assert_counts(result, 3, 2, duplicates=1)
    assert len({item["explanation"] for item in result["findings"]}) == 2


def test_combined_python_counts_and_compact_output_keep_truncation_notice(monkeypatch):
    ruff_findings = [make_finding(i, rule="RUFF:F401") for i in range(1, 43)]
    retained, ruff_stats = select_findings(ruff_findings, 40)
    monkeypatch.setattr(
        "review_agent.tools.ruff_checks",
        lambda code: {
            "status": "completed",
            "findings": [item.model_dump() for item in retained],
            **ruff_stats,
            "truncation_notice": "Ruff 共 42 条，保留 40 条，省略 2 条。",
        },
    )
    code = "\n".join(f"def f{i}(items=[]): pass" for i in range(41))
    result = ToolRegistry(ReviewInput(code=code)).execute("run_static_checks", "{}")
    assert_counts(result, 83, 80)
    compact = compact_tool_result("run_static_checks", result)
    assert_counts(compact, 83, 80)
    assert_counts(compact["ruff"], 42, 40)
    assert "Python AST" in compact["truncation_notice"]
    assert "省略 2 条" in compact["truncation_notice"]


def test_cpp_keeps_late_high_priority_diagnostic_after_forty_lower_items():
    code = (
        "int main() { int value = 0;\n"
        + "\n".join("if (value = 1) {}" for _ in range(40))
        + "\nreturn 1 / 0;\n}\n"
    )
    result = cpp_checks(code)
    assert result["builtin_status"] == "completed"
    assert result["builtin_findings"][0]["rule_id"] == "CPP002"
    assert result["builtin_findings"][0]["line_start"] == 42
    assert_counts(result, 41, 40)
    assert "保留 40 条" in result["truncation_notice"]
    assert "省略 1 条" in result["truncation_notice"]


def test_cpp_syntax_diagnostic_cap_is_counted_and_explicit():
    result = cpp_checks("\n".join(f"int v{i} = ;" for i in range(8)))
    assert result["builtin_status"] == "syntax_error"
    assert_counts(result, 8, 5)
    assert "省略 3 条" in result["truncation_notice"]


def test_legacy_demo_client_sorts_before_thirty_item_cap():
    raw = [make_finding(line).model_dump() for line in range(1, 31)]
    raw.append(make_finding(31, severity="high").model_dump())
    response = DemoClient().complete(
        [
            {
                "role": "user",
                "content": json.dumps(
                    {"tool_results": {"run_static_checks": {"builtin_findings": raw}}}
                ),
            }
        ],
        json_output=True,
    )
    draft = Draft.model_validate_json(response["content"])
    assert len(draft.findings) == 30
    assert draft.findings[0].severity == "high"
    assert "31 条" in draft.summary
    assert any("省略 1 条" in note for note in draft.limitations)
