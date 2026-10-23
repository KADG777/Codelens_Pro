"""Persist public node handoffs and observed durations, never model internals."""

import copy


def handoff(data: dict, node: str) -> dict:
    item = next((v for v in data.get("versions", []) if v["id"] == data.get("current_version")), {})
    result = {"source_version": item.get("label"), "code_hash": item.get("hash"),
              "task": data.get("task"), "acceptance_id": data.get("acceptance_id"),
              "fix_round": data.get("fix_round", 0), "next_node": data.get("next_node"),
              "status": data.get("status"), "termination_reason": data.get("termination_reason", "")}
    for key in ("review", "validation"):
        evidence = data.get(key) or {}
        result[key] = {k: copy.deepcopy(v) for k, v in evidence.items() if k != "raw_report"}
    result["extension_output"] = copy.deepcopy(data.get("node_outputs", {}).get(node, {}))
    if node == "fix":
        result["repair"] = [{k: v for k, v in r.items() if k != "revised_code"}
                            for r in data.get("repair_records", [])[-1:]]
    return result
