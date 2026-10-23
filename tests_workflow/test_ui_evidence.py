"""Evidence level, source identity and transient progress stay distinct."""

from copy import deepcopy

from ui.diff_view import build_side_by_side_html
from ui.evidence import issue_traces, report_markdown
from ui.progress import (
    branch_overlap_evidence,
    build_progress_html,
    result_summary,
    role_cards,
    usage_summary,
)
from ui.workflow_editor import build_plan_svg
from workflow.models import artifact


def example_state():
    old = artifact("def f(x):\n    return missing\n", "solution.py", 0, None, "coding")
    new = artifact("def f(x):\n    return x\n", "solution.py", 1, old["id"], "fix")
    finding = {"id": "F-1", "title": "未定义变量", "rule_id": "F821", "line_start": 2,
               "line_end": 2, "severity": "high", "evidence": "return missing", "explanation": "名称未定义"}
    initial = {"code_hash": old["hash"], "complete": True, "findings": [finding], "pending_findings": []}
    final = {"code_hash": new["hash"], "complete": True, "findings": [], "pending_findings": [], "requirements_status": "met"}
    validation = {"code_hash": new["hash"], "status": "passed", "functional_executed": False,
                  "checks": [{"id": "syntax", "status": "passed"}, {"id": "ruff", "status": "passed"},
                             {"id": "functional", "status": "skipped"}]}
    return {"run_id": "current-run", "task": "修复明确缺陷", "input_mode": "generation", "mode": "demo",
            "status": "completed_static", "phase": "gate", "fix_round": 1, "max_fix_rounds": 2,
            "versions": [old, new], "original_version": old["id"], "current_version": new["id"],
            "review_history": [initial, final], "review": final, "validation": validation,
            "validation_history": [validation], "unresolved": [], "events": [],
            "repair_records": [{"base_code_hash": old["hash"], "revised_code": new["code"],
                                "addressed_finding_ids": ["F-1"], "change_summary": "改用函数参数", "round": 1}],
            "metrics": {"model_calls": 3, "prompt_tokens": 100, "completion_tokens": 50}}


def test_summary_does_not_treat_saved_code_or_static_checks_as_functional_success():
    state = example_state()
    summary = {item["name"]: item for item in result_summary(state)}
    assert summary["源码改动"]["value"] == "已保存修改"
    assert summary["静态检查"]["value"] == "已通过"
    assert summary["功能与效果"]["value"] == "尚未实测"
    state["validation"]["code_hash"] = state["versions"][0]["hash"]
    state["validation"]["functional_executed"] = True
    summary = {item["name"]: item for item in result_summary(state)}
    assert summary["静态检查"]["value"] == "待检查"
    assert summary["功能与效果"]["value"] == "尚未实测"


def test_performance_goal_remains_unmeasured_even_if_functional_cases_pass():
    state = example_state()
    state["task"] = "提升识别准确性能"
    state["validation"]["functional_executed"] = True
    summary = {item["name"]: item for item in result_summary(state)}
    assert "效果仍未测量" in summary["功能与效果"]["detail"]
    assert summary["功能与效果"]["tone"] == "pending"
    assert summary["待确认事项"]["value"] != "无额外待办"


def test_transient_parallel_start_and_branch_finish_never_claim_uncommitted_outputs():
    state = example_state()
    state.update(status="running", review={}, validation={}, execution_event={"kind": "node_start", "node": "review"},
                 active_nodes=["review", "validate"], execution_stage="started")
    cards = {item["node"]: item for item in role_cards(state)}
    assert cards["review"]["state"] == cards["validate"]["state"] == "active"
    rendered = build_progress_html(state)
    assert "审查 Agent、独立验收" in rendered
    state.update(execution_event={"kind": "node_end", "node": "review"}, active_nodes=["validate"], execution_stage="branch_finished")
    cards = {item["node"]: item for item in role_cards(state)}
    assert cards["review"]["state"] == "waiting"
    assert "等待汇合" in cards["review"]["detail"]
    assert cards["validate"]["state"] == "active"


def test_upload_skips_coding_and_paused_or_blocked_node_is_visible_and_escaped():
    state = example_state()
    state.update(input_mode="upload", status="paused", pending_nodes=["validate"])
    cards = {item["node"]: item for item in role_cards(state)}
    assert cards["coding"]["state"] == "skipped"
    assert cards["validate"]["state"] == "paused"
    state.update(status="blocked", phase="fix", error='<img src=x onerror="alert(1)">')
    assert {item["node"]: item for item in role_cards(state)}["fix"]["state"] == "blocked"
    assert "<img" not in build_progress_html(state)


def test_problem_trace_binds_version_real_changed_lines_and_independent_evidence():
    state = example_state()
    trace = issue_traces(state)[0]
    assert state["versions"][0]["hash"] in trace["key"]
    assert trace["target"]["id"] == state["current_version"]
    assert trace["near_blocks"][0]["old_start"] == 2
    assert trace["review_status"] == "复查未再检出同类问题"
    assert "功能未执行" in trace["validation_status"]
    rendered = build_side_by_side_html(trace["source"]["code"], trace["target"]["code"], focus_old_range=(2, 2))
    assert 'id="old-L2"' in rendered
    assert 'class="removed focus"' in rendered


def test_model_claim_without_saved_candidate_does_not_become_a_verified_repair():
    state = example_state()
    state["repair_records"][0]["revised_code"] = state["versions"][0]["code"]
    trace = issue_traces(state)[0]
    assert trace["record"]
    assert not trace["target"]
    assert not trace["near_blocks"]
    assert trace["review_status"] == "尚未形成关联修复版本"
    assert "无关联新版本" in trace["validation_status"]
    state["review_history"][0]["code_hash"] = "unknown-source"
    assert issue_traces(state) == []


def test_usage_uses_full_lineage_and_report_excludes_connection_settings():
    state = example_state()
    state["parent_run_id"] = "parent-run"
    state["inherited_metrics"] = {"model_calls": 2, "prompt_tokens": 20, "completion_tokens": 10}
    state["inherited_total_metrics"] = {"model_calls": 5, "prompt_tokens": 50, "completion_tokens": 25}
    usage = usage_summary(state)
    assert usage["current"]["model_calls"] == 3
    assert usage["total"]["model_calls"] == 8
    state["settings"] = {"api_key": "must-never-export", "base_url": "https://secret.example"}
    report = report_markdown(state)
    assert "must-never-export" not in report and "secret.example" not in report
    assert "parent-run" in report
    assert state["versions"][-1]["hash"] in report
    assert "非单问题证明" not in report  # Functional execution did not happen.


def test_editor_preview_distinguishes_parallel_and_disabled_repair_paths():
    parallel = build_plan_svg({"parallel_checks": True, "enable_repair": True, "max_fix_rounds": 2})
    disabled = build_plan_svg({"parallel_checks": False, "enable_repair": False, "max_fix_rounds": 2})
    assert "并行工作流预览" in parallel
    assert "汇合" in parallel
    assert "顺序工作流预览" in disabled
    assert 'opacity=".45"' in disabled
    assert "完成 / 待处理" in parallel


def test_reused_finding_ids_in_different_versions_remain_distinct():
    state = example_state()
    same_id = deepcopy(state["review_history"][0]["findings"][0])
    same_id["title"] = "另一个未定义变量"
    state["review_history"][-1]["findings"] = [same_id]
    state["review"]["findings"] = [same_id]
    traces = issue_traces(state)
    assert len(traces) == 2
    assert traces[0]["key"] != traces[1]["key"]
    assert traces[0]["review_status"] == "复查仍检出同类问题"


def test_parallel_proof_requires_measured_overlap_for_the_same_version():
    state = {"branch_timings": [
        {"node": "review", "code_hash": "same", "start_clock": 10.0, "end_clock": 12.0},
        {"node": "validate", "code_hash": "same", "start_clock": 10.5, "end_clock": 11.5},
        {"node": "validate", "code_hash": "different", "start_clock": 10.5, "end_clock": 11.5},
        {"node": "validate", "code_hash": "same", "start_clock": 20.0, "end_clock": 21.0},
    ]}
    actual = branch_overlap_evidence(state)
    assert len(actual) == 1
    assert actual[0]["overlap_seconds"] == 1.0
    assert branch_overlap_evidence({"workflow_config": {"parallel_checks": True}}) == []
