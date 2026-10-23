"""Explicit contracts and actual overlapping branches, without model requests."""

import copy
import threading
import time

import pytest

from workflow import WorkflowRunService
from workflow.examples import ORDER_HARNESS, ORDER_TASK
from workflow.models import source_hash
from workflow.registry import build_harness, get_acceptance_hash, get_task_spec, list_task_specs


def review_result(code_hash):
    return {"code_hash": code_hash, "complete": True, "findings": [], "pending_findings": [],
            "summary": "Controlled result", "requirements_status": "met"}


def validation_result(code_hash, status="passed"):
    return {"code_hash": code_hash, "status": status, "functional_executed": False,
            "checks": [{"id": "controlled", "name": "controlled", "status": status, "detail": status}],
            "passed_count": int(status == "passed"), "failed_count": int(status == "failed")}


def test_registry_has_ten_separate_contracts_and_preserves_legacy_hash():
    public = list_task_specs()
    assert len(public) == 10
    assert len({item["id"] for item in public}) == 10
    assert get_acceptance_hash("orders-v1") == source_hash(ORDER_HARNESS)
    for item in public:
        spec = get_task_spec(item["id"])
        assert spec.public_cases and spec.holdout_cases
        assert "holdout" not in spec.model_contract()
        assert "reference_code" not in spec.model_contract()
        assert "correct_code" not in spec.model_contract()
        assert build_harness(spec.id, holdout=True) != build_harness(spec.id)


def test_explicit_contract_survives_reworded_task_and_second_demo_works(tmp_path):
    service = WorkflowRunService(tmp_path / "registry.db")
    run_id = service.create_run("请完成所选文本统计合同。", acceptance_id="text-stats-v1")
    created = service.get_snapshot(run_id)
    assert created["acceptance_id"] == "text-stats-v1"
    assert set(created["acceptance_spec"]) == {"id", "label", "task", "entrypoint", "parameters"}
    assert all(case.id not in str(created["acceptance_spec"]) for case in get_task_spec("text-stats-v1").holdout_cases)
    result = service.execute(run_id)
    assert result["status"] == "completed_static"
    assert result["fix_round"] == 1
    assert len(result["versions"]) == 2
    assert result["versions"][-1]["code"] == get_task_spec("text-stats-v1").reference_code


def test_unknown_explicit_contract_fails_before_creating_run(tmp_path):
    service = WorkflowRunService(tmp_path / "unknown.db")
    with pytest.raises(ValueError, match="未注册"):
        service.create_run("custom", acceptance_id="not-registered")
    assert service.list_runs() == []


def test_start_callback_uses_previous_committed_source(tmp_path):
    service = WorkflowRunService(tmp_path / "events.db")
    run_id = service.create_run(ORDER_TASK, scenario="clean")
    events = []
    result = service.execute(run_id, on_event=lambda state: events.append(copy.deepcopy(state)))
    coding_start = next(state for state in events if state["execution_event"] == {"kind": "node_start", "node": "coding"})
    coding_end = next(state for state in events if state["execution_event"] == {"kind": "node_end", "node": "coding"})
    assert coding_start["transient"] is True and coding_start["active_nodes"] == ["coding"]
    assert coding_start["versions"] == [] and coding_start["current_version"] is None
    assert coding_end["transient"] is False and len(coding_end["versions"]) == 1
    assert result["status"] == "completed_static"
    assert "execution_event" not in service.get_snapshot(run_id)


def test_parallel_checks_overlap_join_and_pause_without_worker_ui_callbacks(tmp_path, monkeypatch):
    barrier = threading.Barrier(2)
    branches = []

    def fake_review(data, code, settings, factory, started):
        branches.append("review")
        barrier.wait(timeout=3)
        time.sleep(0.03)
        return review_result(source_hash(code))

    def fake_validation(code, code_hash, profile, acceptance_id, **kwargs):
        branches.append("validate")
        barrier.wait(timeout=3)
        time.sleep(0.03)
        return validation_result(code_hash)

    monkeypatch.setattr("workflow.agents.review", fake_review)
    service = WorkflowRunService(tmp_path / "parallel.db", validation_runner=fake_validation)
    run_id = service.create_run("Inspect existing source", source="def identity(value):\n    return value\n",
                                acceptance_id="none", workflow_config={"parallel_checks": True, "pause_after_review": True})
    callbacks, callback_threads = [], []

    def on_event(state):
        callbacks.append(state)
        callback_threads.append(threading.get_ident())

    paused = service.execute(run_id, on_event=on_event)
    assert paused["status"] == "paused" and paused["pending_nodes"] == ["gate"]
    assert sorted(branches) == ["review", "validate"]
    first, second = paused["branch_timings"]
    assert max(first["start_clock"], second["start_clock"]) < min(first["end_clock"], second["end_clock"])
    assert any(set(item["active_nodes"]) == {"review", "validate"} for item in callbacks)
    assert set(callback_threads) == {threading.get_ident()}
    assert paused["review"]["code_hash"] == paused["validation"]["code_hash"]
    result = WorkflowRunService(tmp_path / "parallel.db", validation_runner=fake_validation).execute(run_id)
    assert result["status"] == "completed_static"
    assert sorted(branches) == ["review", "validate"]


def test_parallel_environment_resume_reuses_successful_review(tmp_path, monkeypatch):
    calls = {"review": 0, "validate": 0}

    def fake_review(data, code, settings, factory, started):
        calls["review"] += 1
        return review_result(source_hash(code))

    def fake_validation(code, code_hash, profile, acceptance_id, **kwargs):
        calls["validate"] += 1
        return validation_result(code_hash, "error" if calls["validate"] == 1 else "passed")

    monkeypatch.setattr("workflow.agents.review", fake_review)
    service = WorkflowRunService(tmp_path / "resume.db", validation_runner=fake_validation)
    run_id = service.create_run("Inspect", source="value = 1\n", acceptance_id="none",
                                workflow_config={"parallel_checks": True})
    blocked = service.execute(run_id)
    assert blocked["status"] == "blocked" and blocked["recoverable"]
    result = service.resume(run_id)
    assert result["status"] == "completed_static"
    assert calls == {"review": 1, "validate": 2}


def test_visual_config_can_disable_repair_without_running_fix(tmp_path):
    service = WorkflowRunService(tmp_path / "no-fix.db")
    run_id = service.create_run(ORDER_TASK, workflow_config={"enable_repair": False})
    result = service.execute(run_id)
    assert result["status"] == "needs_attention"
    assert result["termination_reason"] == "repair_disabled"
    assert not any(item["node"] == "fix" for item in result["events"])


@pytest.mark.parametrize("invalid", [
    {"parallel_checks": "true"}, {"enable_repair": 0}, {"pause_after_review": 1},
    {"max_fix_rounds": True}, {"unexpected_node": True},
])
def test_visual_config_rejects_coerced_booleans_and_unknown_fields_before_run(tmp_path, invalid):
    service = WorkflowRunService(tmp_path / "invalid-plan.db")
    with pytest.raises(ValueError):
        service.create_run(ORDER_TASK, workflow_config=invalid)
    assert service.list_runs() == []


def test_pause_at_terminal_gate_does_not_turn_completion_into_resumable_work(tmp_path):
    service = WorkflowRunService(tmp_path / "terminal-pause.db")
    run_id = service.create_run(ORDER_TASK, scenario="clean")
    result = service.execute(run_id, stop_after="gate")
    assert result["status"] == "completed_static"
    assert result["pending_nodes"] == []
    repeated_events = []
    repeated = service.resume(run_id, on_event=repeated_events.append)
    assert repeated == result
    assert repeated_events == []
