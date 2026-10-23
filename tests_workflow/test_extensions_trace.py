from copy import deepcopy

import pytest

from ui.evidence import issue_traces
from ui.execution_graph import build_execution_svg, node_states
from workflow import WorkflowRunService
from workflow.examples import ORDER_TASK
from workflow.node_registry import NodeOutput, NodeRegistry, NodeSpec, default_registry


@pytest.mark.parametrize("profile,contract", [("functional_portable", "none"), ("functional_required", "none"),
                                             ("functional_required", "text-stats-v1")])
def test_invalid_acceptance_never_creates_a_run_or_calls_model(tmp_path, profile, contract):
    def forbidden(*args):
        pytest.fail("Must fail before a model call")
    with_service = WorkflowRunService(tmp_path / "preflight.db", model_factory=forbidden)
    with pytest.raises(ValueError):
        with_service.create_run("custom", source="x = 1\n", mode="live", validation_profile=profile, acceptance_id=contract)
    assert with_service.list_runs() == []
    with_service.close()


def test_old_invalid_run_stops_instead_of_becoming_retryable(tmp_path):
    service = WorkflowRunService(tmp_path / "old.db")
    run = service.create_run("custom", source="x = 1\n", acceptance_id="none")
    state = service.get_snapshot(run)
    state.update(status="blocked", recoverable=True, validation_profile="functional_portable")
    service._graph().update_state(service._config(run), {"data": state}, as_node="dispatch")
    result = service.execute(run)
    assert result["termination_reason"] == "invalid_configuration"
    assert not result["recoverable"] and result["metrics"]["model_calls"] == 0
    assert service.execute(run)["status"] == "needs_attention"
    service.close()


def test_new_role_runs_without_dispatch_edits_and_cannot_mutate_core_state(tmp_path):
    registry = NodeRegistry()
    def handler(request, context):
        request.state["validation"]["status"] = "forged"
        return NodeOutput(code_hash=request.code_hash, summary="A separately registered role")
    registry.register(NodeSpec("test_observer", "2.1", "Observer", "Independent extension", handler))
    service = WorkflowRunService(tmp_path / "extension.db", node_registry=registry)
    run = service.create_run(ORDER_TASK, scenario="clean", workflow_config={"extension_nodes": ["test_observer"]})
    paused = service.execute(run, stop_after="test_observer")
    assert paused["status"] == "paused" and paused["pending_nodes"] == ["gate"]
    assert paused["validation"]["status"] == "passed"
    assert paused["node_outputs"]["test_observer"]["node_version"] == "2.1"
    service.close()
    service = WorkflowRunService(tmp_path / "extension.db", node_registry=registry)
    final = service.resume(run)
    assert final["status"] == "completed_static"
    assert len(final["node_output_history"]) == 1
    records = final["node_runs"]
    assert all(r["duration_seconds"] >= 0 and r["input"] and r["output"] for r in records)
    assert next(r for r in records if r["node"] == "test_observer")["output"]["next_node"] == "gate"
    service.close()


@pytest.mark.parametrize("bad", ["stale", "forged"])
def test_extension_stale_or_overbroad_output_fails_closed(tmp_path, bad):
    registry = NodeRegistry()
    def handler(request, context):
        return {"code_hash": "old" if bad == "stale" else request.code_hash,
                "summary": "bad", **({"status": "completed_verified"} if bad == "forged" else {})}
    registry.register(NodeSpec("bad_role", "1", "Bad", "fixture", handler))
    service = WorkflowRunService(tmp_path / "bad.db", node_registry=registry)
    run = service.create_run(ORDER_TASK, scenario="clean", workflow_config={"extension_nodes": ["bad_role"]})
    result = service.execute(run)
    assert result["termination_reason"] == "invalid_output" and not result["node_outputs"]
    assert result["node_runs"][-1]["error"]
    service.close()


def test_extension_version_mismatch_is_not_retryable(tmp_path):
    service = WorkflowRunService(tmp_path / "version.db")
    run = service.create_run(ORDER_TASK, workflow_config={"extension_nodes": ["acceptance_explainer"]})
    service.node_registry = NodeRegistry()
    result = service.execute(run)
    assert result["termination_reason"] == "invalid_configuration" and not result["recoverable"]
    service.close()


def test_shipped_agent_is_registered_and_parallel_handoffs_survive(tmp_path):
    service = WorkflowRunService(tmp_path / "shipped.db", node_registry=default_registry())
    run = service.create_run(ORDER_TASK, workflow_config={"parallel_checks": True, "extension_nodes": ["acceptance_explainer"]})
    result = service.execute(run)
    assert result["status"] == "completed_static"
    assert len(result["node_output_history"]) == 2
    branches = [r for r in result["node_runs"] if r.get("parallel")]
    assert len(branches) == 4
    assert all(r["input"]["code_hash"] == r["output"]["code_hash"] for r in branches)
    service.close()


def test_failed_case_trace_distinguishes_claim_lineage_and_actual_retest():
    from tests_workflow.test_ui_evidence import example_state
    state = example_state()
    old, new = state["versions"]
    initial = {"code_hash": old["hash"], "functional_executed": True,
               "checks": [{"id": "boundary", "name": "边界", "status": "failed", "detail": "expected 1 got 0"}]}
    final = {"code_hash": new["hash"], "functional_executed": True,
             "checks": [{"id": "boundary", "status": "passed"}]}
    state.update(validation_history=[initial, final], validation=final)
    trace = [t for t in issue_traces(state) if t.get("kind") == "validation"][0]
    assert trace["target"] == new and trace["link_basis"] == "lineage"
    assert "实际执行" in trace["validation_status"] and "line_start" not in trace["finding"]
    state["repair_records"][0]["addressed_finding_ids"].append("V-boundary")
    assert issue_traces(state)[-1]["link_basis"] == "claim"
    final["functional_executed"] = False
    assert "未完成" in issue_traces(state)[-1]["validation_status"]
    final["code_hash"] = "stale"
    assert "缺少" in issue_traces(state)[-1]["validation_status"]


def test_runtime_graph_uses_observed_routes_and_escapes_failure():
    state = {"run_id": "a", "status": "running", "active_nodes": ["fix"], "node_runs": [
        {"node": "gate", "duration_seconds": .1, "output": {"next_node": "fix"}, "error": ""},
        {"node": "review", "duration_seconds": .2, "output": {}, "error": "<script>bad</script>"}]}
    html = build_execution_svg({}, state)
    assert 'data-edge="gate:fix"' in html and "#2267d7" in html
    assert "<script>" not in html and "&lt;script&gt;" in html
    assert node_states(deepcopy(state))["review"] == "blocked"


def test_paused_summary_does_not_hide_known_failures_before_gate():
    from tests_workflow.test_ui_evidence import example_state
    from ui.progress import result_summary
    state = example_state()
    state.update(status="paused", unresolved=[])
    state["validation"]["checks"].append({"id": "boundary", "status": "failed", "detail": "边界仍失败"})
    summary = {item["name"]: item for item in result_summary(state)}
    assert summary["待确认事项"]["value"] == "1 项"
