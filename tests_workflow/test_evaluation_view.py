"""Saved report presentation never turns missing execution into success."""

import hashlib
import json
from copy import deepcopy

import pytest

from ui.evaluation_view import audit_evidence, evaluation_totals, load_saved_report


def _entry(case_id, *, initial="failed", expectation="repair", input_mode="upload"):
    before = "def f(x):\n    return x - 1\n" if initial == "failed" else "def f(x):\n    return x\n"
    after = "def f(x):\n    return x\n"

    def audit(code, status):
        return {"executed": True, "status": status, "code_hash": hashlib.sha256(code.encode()).hexdigest(),
                "checks": [{"id": "example", "status": status, "expected": "1", "actual": 1 if status == "passed" else 0}],
                "holdout": True, "backend": "synthetic-test"}

    return {"id": case_id, "task_id": "orders-v1", "task": "合成测试任务", "case_status": "completed",
            "input_mode": input_mode, "expectation": expectation, "initial_code": before, "final_code": after,
            "initial_audit": audit(before, initial), "final_audit": audit(after, "passed"),
            "workflow_status": "completed_verified", "termination_reason": "frozen_acceptance_passed",
            "roles_executed": ["coding", "review"] if input_mode == "generation" else ["review", "fix"],
            "all_three_roles_executed": False, "metrics": {"model_calls": 2}, "snapshot": {"unresolved": []}}


def _report():
    safe = _entry("unmeasured", initial="passed", expectation="safe_stop")
    safe.update(workflow_status="needs_attention", termination_reason="unresolved_review", roles_executed=["review"])
    safe["snapshot"]["unresolved"] = ["效果未测，不能确认目标达成。"]
    entries = [_entry("repair"), _entry("generate", initial="passed", input_mode="generation"),
               _entry("clean", initial="passed", expectation="clean_control"), safe]
    return {"schema_version": 1, "evaluation_mode": "real_live", "real_api_used": True,
            "planned_case_ids": [entry["id"] for entry in entries], "entries": entries,
            "summary": {"actual_model_calls": 8, "actual_prompt_tokens": 100, "actual_completion_tokens": 20,
                        "workflow_metrics": {"active_seconds": 1.25}, "three_role_generation_cases": []}}


def _write_report(root, report, name="workflow-live.json"):
    target = root / "evals" / "results" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False), encoding="utf-8")


def test_saved_evaluation_counts_before_after_and_safe_stop_separately():
    totals = evaluation_totals(_report())
    assert totals["planned"] == 4 and totals["eligible"] == 3
    assert totals["stages"]["initial"]["passed"] == 2
    assert totals["stages"]["final"]["passed"] == 3
    assert totals["repair_successes"] == totals["repair_opportunities"] == 1
    assert totals["verified"] == 3 and totals["safe_stops"] == 1
    assert totals["regressions"] == totals["changed_clean"] == 0


def test_unexecuted_mismatched_or_empty_audit_cannot_count_as_functional_success():
    report = _report()
    entry = report["entries"][0]
    entry["final_audit"]["executed"] = False
    assert audit_evidence(entry, "final")["status"] == "unavailable"
    assert evaluation_totals(report)["verified"] == 2
    entry["final_audit"]["executed"] = True
    entry["final_audit"]["code_hash"] = "old-source"
    assert audit_evidence(entry, "final")["status"] == "unavailable"
    entry["final_audit"]["code_hash"] = hashlib.sha256(entry["final_code"].encode()).hexdigest()
    entry["final_audit"]["checks"] = []
    assert audit_evidence(entry, "final")["status"] == "unavailable"


def test_three_roles_require_executed_roles_and_offline_usage_is_separate():
    report = _report()
    report["entries"][1]["all_three_roles_executed"] = True
    report["summary"]["three_role_generation_cases"] = ["generate"]
    assert evaluation_totals(report)["three_role_generation_cases"] == []
    report["real_api_used"] = False
    report["evaluation_mode"] = "offline_smoke"
    report["summary"].update(actual_model_calls=0, actual_prompt_tokens=0, actual_completion_tokens=0, scripted_model_calls=66)
    assert evaluation_totals(report)["actual_model_calls"] == 0
    assert evaluation_totals(report)["actual_tokens"] == 0
    assert evaluation_totals(report)["scripted_model_calls"] == 66


def test_report_loader_uses_fixed_paths_and_does_not_execute_saved_source(tmp_path):
    report = _report()
    report["entries"][0]["final_code"] = 'raise RuntimeError("this source must never run")\n'
    _write_report(tmp_path, report)
    loaded, digest = load_saved_report(tmp_path, "public_live")
    assert loaded["entries"][0]["final_code"].startswith("raise RuntimeError")
    assert len(digest) == 64
    with pytest.raises(ValueError, match="固定"):
        load_saved_report(tmp_path, "../reports/private.json")
    with pytest.raises(ValueError, match="尚未保存"):
        load_saved_report(tmp_path, "local_live")


def test_safe_stop_label_does_not_hide_budget_or_runtime_failure():
    report = _report()
    report["entries"][-1]["termination_reason"] = "budget_exhausted"
    assert evaluation_totals(report)["safe_stops"] == 0


def test_evaluation_page_shows_evidence_and_download_without_model_calls(tmp_path):
    from streamlit.testing.v1 import AppTest

    _write_report(tmp_path, _report())
    script = "from pathlib import Path\nfrom ui.evaluation_view import render_evaluation_page\nrender_evaluation_page(Path(" + repr(str(tmp_path)) + "))\n"
    app = AppTest.from_string(script, default_timeout=30).run()
    assert not app.exception, [item.value for item in app.exception]
    metrics = {item.label: item.value for item in app.metric}
    assert metrics["功能验收完成"] == "3 / 4"
    assert metrics["真实 API 调用"] == "8"
    assert metrics["安全停机"] == "1"
    assert any("三个角色全部执行" in item.value for item in app.info)
    assert app.get("iframe")
    assert len(app.get("download_button")) == 1


def test_offline_page_keeps_simulated_usage_distinct_and_handles_missing_report(tmp_path):
    from streamlit.testing.v1 import AppTest

    offline = deepcopy(_report())
    offline.update(real_api_used=False, evaluation_mode="offline_smoke")
    offline["summary"].update(actual_model_calls=0, actual_prompt_tokens=0, actual_completion_tokens=0, scripted_model_calls=66)
    _write_report(tmp_path, offline, "workflow-smoke.json")
    script = "from pathlib import Path\nfrom ui.evaluation_view import render_evaluation_page\nrender_evaluation_page(Path(" + repr(str(tmp_path)) + "))\n"
    app = AppTest.from_string(script, default_timeout=30).run()
    assert not app.exception
    assert any("尚未保存" in item.value for item in app.info)
    app.selectbox(key="flow_eval_source").set_value("public_offline").run()
    assert not app.exception, [item.value for item in app.exception]
    metrics = {item.label: item.value for item in app.metric}
    assert metrics["真实 API 调用"] == "0"
    assert metrics["真实模型 token"] == "0"
    assert any("模拟角色调用 66" in item.value for item in app.caption)
