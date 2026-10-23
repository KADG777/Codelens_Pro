"""Checkpoint one executable wave; retain successful siblings on recovery."""

import copy
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone

from workflow.canvas import CanvasGraph, edge_taken, predicate_value, ready_nodes
from workflow.tracing import handoff

TERMINAL = {"completed_static", "completed_verified", "needs_attention", "failed"}


def step(service, state, settings, on_event):
    data = copy.deepcopy(state["data"])
    graph = CanvasGraph.model_validate(data["workflow_config"]["canvas"])
    cursor = data.setdefault("canvas_cursor", {"outcomes": {}, "repair": False})
    outcomes = cursor["outcomes"]
    if data["input_mode"] == "upload" and not outcomes:
        outcomes["coding"] = "done"
    data.update(status="running", next_node="canvas", error="", recoverable=False)
    names = ["fix"] if cursor["repair"] else ready_nodes(graph, outcomes)
    if not names:
        data.update(status="needs_attention", termination_reason="invalid_canvas_state", error="画布没有可执行节点。")
        return {"data": data}
    nodes = {n.id: n for n in graph.nodes}
    base = copy.deepcopy(data)
    started = time.monotonic()
    model_nodes = [n for n in names if n not in {"validate", "gate"} and nodes[n].kind != "condition"]
    results = {}

    def run(name):
        branch = copy.deepcopy(base)
        if nodes[name].kind == "condition":
            now = datetime.now(timezone.utc).isoformat()
            value = "true" if predicate_value(nodes[name], branch) else "false"
            record = {"id": uuid.uuid4().hex, "node": name, "round": data["fix_round"],
                      "started_at": now, "finished_at": now, "duration_seconds": 0.0,
                      "status": "running", "error": "", "input": handoff(branch, name),
                      "output": {**handoff(branch, name), "predicate": nodes[name].predicate, "decision": value}, "usage": {}}
            branch["node_runs"].append(record)
            branch["events"].append({"id": uuid.uuid4().hex, "node": name, "status": "running",
                                     "time": now, "message": f"条件分支：{value}", "version": branch["current_version"]})
            branch["canvas_decision"] = value
            return branch
        # Concurrent model branches receive disjoint shares of the remaining budget.
        if len(model_nodes) > 1 and name in model_nodes:
            metrics, limits = branch["metrics"], branch["budget_limits"]
            for key, used in (("max_model_calls", metrics["model_calls"]),
                              ("max_tokens", metrics["prompt_tokens"] + metrics["completion_tokens"])):
                limits[key] = used + int((limits[key] - used) // len(model_nodes))
        return service._node(name, settings, None)({"data": branch})["data"]

    service._notify(data, on_event, kind="node_start", node=names[0], active_nodes=names)
    if len(names) == 1:
        results[names[0]] = run(names[0])
    else:
        with ThreadPoolExecutor(max_workers=min(8, len(names))) as pool:
            pending = {pool.submit(run, n): n for n in names}
            while pending:
                done, _ = wait(pending, return_when=FIRST_COMPLETED)
                for future in done:
                    name = pending.pop(future)
                    results[name] = future.result()
                    preview = copy.deepcopy(base)
                    preview["node_runs"] += [r for result in results.values() for r in result["node_runs"][len(base["node_runs"]):]]
                    service._notify(preview, on_event, kind="branch_end", node=name, active_nodes=list(pending.values()))
    if len(names) == 1:
        data = results[names[0]]
        data["budget_limits"] = base["budget_limits"]
    else:
        # Source writers never share a wave. Read-only outputs merge by namespace.
        for name in names:
            result = results[name]
            if name == "review":
                data["review"] = result["review"]
                data["inspection_cycles"] = result["inspection_cycles"]
            if name == "validate":
                data["validation"] = result["validation"]
            if name in graph.extensions():
                data["node_outputs"].update(result["node_outputs"])
            for key in ("node_runs", "events", "review_history", "validation_history", "node_output_history"):
                data[key].extend(result[key][len(base[key]):])
            for key in data["metrics"]:
                if key != "active_seconds":
                    data["metrics"][key] += result["metrics"][key] - base["metrics"][key]
    data["metrics"]["active_seconds"] = round(base["metrics"]["active_seconds"] + time.monotonic() - started, 6)
    data["canvas_cursor"] = cursor
    data["next_node"] = "canvas"
    for name, result in results.items():
        if result["status"] not in {"blocked", "needs_attention", "failed"}:
            outcomes[name] = result.get("canvas_decision", "done") if nodes[name].kind == "condition" else "done"
    faults = [(n, r) for n, r in results.items() if r["status"] in {"blocked", "needs_attention", "failed"}]
    if faults:
        name, result = next(((n, r) for n, r in faults if r["status"] in TERMINAL), faults[0])
        for key in ("status", "error", "recoverable", "termination_reason", "unresolved"):
            data[key] = result[key]
        data["phase"] = name
        if name == "gate" and data["status"] in TERMINAL:
            cursor["terminal_edge"] = "done"
    elif "gate" in names:
        if data["status"] in TERMINAL:
            cursor["terminal_edge"] = "done"
        else:
            cursor["repair"] = True
            cursor["terminal_edge"] = "repair"
    elif "fix" in names:
        cursor.update(outcomes={"coding": "done"}, repair=False, terminal_edge="")
        data["node_outputs"] = {}
    else:
        data["phase"] = names[-1]
    observed = data.setdefault("canvas_edges", [])
    for edge in graph.edges:
        taken = (edge.source == "gate" and edge.when == cursor.get("terminal_edge")) or (
            edge.source == "fix" and "fix" in names and not faults) or (
            edge.source not in {"gate", "fix"} and edge_taken(edge, cursor["outcomes"]))
        record = {"source": edge.source, "target": edge.target, "when": edge.when, "round": data["fix_round"] if "fix" in names and not faults else base["fix_round"]}
        if taken and record not in observed:
            observed.append(record)
    for record in data["node_runs"][len(base["node_runs"]):]:
        record["parallel"] = len(names) > 1
        record["output"]["next_nodes"] = [e.target for e in graph.edges if e.source == record["node"] and
                                           any(o["source"] == e.source and o["target"] == e.target and o["when"] == e.when and o["round"] == (data["fix_round"] if "fix" in names and not faults else base["fix_round"]) for o in observed)]
        record["output"]["next_node"] = "canvas"
    if data["metrics"]["active_seconds"] > data["budget_limits"]["max_active_seconds"]:
        data.update(status="needs_attention", recoverable=False, termination_reason="budget_exhausted", error="活动时间预算耗尽。")
    return {"data": data}
