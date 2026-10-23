from copy import deepcopy
from threading import Barrier

import pytest

from ui.execution_graph import build_execution_svg
from workflow import WorkflowRunService
from workflow.canvas import CanvasGraph, template_graph
from workflow.examples import ORDER_TASK
from workflow.node_registry import NodeOutput, NodeSpec, default_registry


def graph(parallel=False, extensions=()):
    return template_graph(parallel, extensions)


@pytest.mark.parametrize("parallel", [True, False])
def test_canvas_executes_frozen_graph_and_repairs(tmp_path, parallel):
    service = WorkflowRunService(tmp_path / "canvas.db")
    run = service.create_run(ORDER_TASK, workflow_config={"canvas": graph(parallel, ["acceptance_explainer"])})
    result = service.execute(run)
    assert result["status"] == "completed_static"
    assert result["fix_round"] == 1
    assert len(result["versions"]) == 2
    names = [r["node"] for r in result["node_runs"]]
    assert names.count("coding") == 1 and names.count("review") == names.count("validate") == 2
    assert names.count("acceptance_explainer") == 2
    assert result["canvas_hash"] == CanvasGraph.model_validate(result["workflow_config"]["canvas"]).digest()
    service.close()


def test_rewired_validation_runs_before_review(tmp_path):
    plan = graph()
    for edge in plan["edges"]:
        edge["source"] = {"review": "validate", "validate": "review"}.get(edge["source"], edge["source"])
        edge["target"] = {"review": "validate", "validate": "review"}.get(edge["target"], edge["target"])
    service = WorkflowRunService(tmp_path / "order.db")
    run = service.create_run(ORDER_TASK, scenario="clean", workflow_config={"canvas": plan})
    result = service.execute(run)
    assert [r["node"] for r in result["node_runs"]] == ["coding", "validate", "review", "gate"]
    service.close()


def conditional_plan():
    plan = graph(False, ["acceptance_explainer"])
    plan["nodes"].append({"id": "condition_risk", "kind": "condition", "predicate": "validation_failed", "x": 400.0, "y": 400.0})
    plan["edges"] = [e for e in plan["edges"] if e["source"] != "validate"]
    plan["edges"] += [{"source": "validate", "target": "condition_risk", "when": "always"},
                      {"source": "condition_risk", "target": "acceptance_explainer", "when": "true"},
                      {"source": "condition_risk", "target": "gate", "when": "false"}]
    return plan


def test_condition_skips_agent_and_joins_successfully(tmp_path):
    service = WorkflowRunService(tmp_path / "condition.db")
    run = service.create_run(ORDER_TASK, scenario="clean", workflow_config={"canvas": conditional_plan()})
    result = service.execute(run)
    assert result["status"] == "completed_static"
    assert result["canvas_cursor"]["outcomes"]["acceptance_explainer"] == "skipped"
    assert not result["node_outputs"]
    branch = next(r for r in result["node_runs"] if r["node"] == "condition_risk")
    assert branch["output"]["decision"] == "false"
    assert branch["output"]["next_nodes"] == ["gate"]
    service.close()


@pytest.mark.parametrize("defect", ["cycle", "missing", "bypass", "stale_read", "wrong_loop", "unknown"])
def test_bad_graph_rejected_before_creating_run(tmp_path, defect):
    plan = graph(False, ["acceptance_explainer"])
    if defect == "cycle":
        plan["edges"].append({"source": "validate", "target": "review"})
    elif defect == "missing":
        plan["nodes"] = [n for n in plan["nodes"] if n["id"] != "validate"]
    elif defect == "bypass":
        plan["edges"].append({"source": "review", "target": "finish"})
    elif defect == "stale_read":
        for edge in plan["edges"]:
            edge["source"] = {"validate": "acceptance_explainer", "acceptance_explainer": "validate"}.get(edge["source"], edge["source"])
            edge["target"] = {"validate": "acceptance_explainer", "acceptance_explainer": "validate"}.get(edge["target"], edge["target"])
    elif defect == "wrong_loop":
        next(e for e in plan["edges"] if e["source"] == "fix")["target"] = "validate"
    else:
        plan["nodes"].append({"id": "not_installed", "x": 0.0, "y": 0.0})
    service = WorkflowRunService(tmp_path / "invalid.db")
    with pytest.raises(ValueError):
        service.create_run(ORDER_TASK, workflow_config={"canvas": plan})
    assert service.list_runs() == []
    service.close()


def test_pause_reopen_and_resume_does_not_rerun_review(tmp_path):
    path = tmp_path / "resume.db"
    service = WorkflowRunService(path)
    run = service.create_run(ORDER_TASK, scenario="clean", workflow_config={"canvas": graph(), "pause_after_review": True})
    paused = service.execute(run)
    assert paused["status"] == "paused"
    assert [r["node"] for r in paused["node_runs"]] == ["coding", "review"]
    service.close()
    service = WorkflowRunService(path)
    result = service.resume(run)
    assert result["status"] == "completed_static"
    assert [r["node"] for r in result["node_runs"]].count("review") == 1
    service.close()


def test_parallel_extensions_overlap_and_failed_sibling_only_retries(tmp_path):
    registry = default_registry()
    barrier = Barrier(2)
    calls = {"observer_a": 0, "observer_b": 0}
    def handler(name):
        def execute(request, context):
            calls[name] += 1
            if calls[name] == 1:
                barrier.wait(timeout=5)
            if name == "observer_b" and calls[name] == 1:
                raise OSError("injected transport failure")
            return NodeOutput(code_hash=request.code_hash, summary=name)
        return execute
    for name in calls:
        registry.register(NodeSpec(name, "1", name, "fixture", handler(name)))
    plan = graph()
    plan["nodes"] += [{"id": n, "x": 800.0, "y": float(i*100)} for i,n in enumerate(calls)]
    plan["edges"] = [e for e in plan["edges"] if not (e["source"] == "validate" and e["target"] == "gate")]
    for name in calls:
        plan["edges"] += [{"source": "validate", "target": name}, {"source": name, "target": "gate"}]
    service = WorkflowRunService(tmp_path / "parallel.db", node_registry=registry)
    run = service.create_run(ORDER_TASK, scenario="clean", workflow_config={"canvas": plan})
    blocked = service.execute(run)
    assert blocked["status"] == "blocked" and calls == {"observer_a": 1, "observer_b": 1}
    service.close()
    service = WorkflowRunService(tmp_path / "parallel.db", node_registry=registry)
    result = service.resume(run)
    assert result["status"] == "completed_static" and calls == {"observer_a": 1, "observer_b": 2}
    assert set(result["node_outputs"]) == set(calls)
    service.close()


def test_saved_canvas_cannot_be_changed_mid_run(tmp_path):
    service = WorkflowRunService(tmp_path / "tampered.db")
    run = service.create_run(ORDER_TASK, workflow_config={"canvas": graph()})
    state = deepcopy(service.get_snapshot(run))
    state["workflow_config"]["canvas"]["nodes"][0]["x"] += 1
    service._graph().update_state(service._config(run), {"data": state}, as_node="dispatch")
    assert service.execute(run)["termination_reason"] == "invalid_configuration"
    service.close()


def test_canvas_svg_uses_saved_positions():
    plan = {"canvas": graph()}
    plan["canvas"]["nodes"][0].update(x=123.0, y=456.0)
    assert 'x="123.0" y="456.0"' in build_execution_svg(plan)


def test_canvas_run_highlights_only_current_round_and_next_paused_node():
    from ui.execution_graph import node_states
    plan = {"canvas": graph()}
    state = {"workflow_config": plan, "status": "paused", "pending_nodes": ["canvas"], "fix_round": 1,
             "canvas_cursor": {"outcomes": {"coding": "done", "review": "done"}, "repair": False},
             "canvas_edges": [{"source": "review", "target": "validate", "round": 0}]}
    assert node_states(state)["validate"] == "paused"
    svg = build_execution_svg(plan, state)
    assert 'data-edge="review:validate"' in svg
    assert 'stroke="#198264"' not in svg
