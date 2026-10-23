"""One bounded review request, executable counterexamples, and regression evidence.

Probe expectations are model-authored, NOT independent acceptance or hidden tests.
Observed mismatches remain advisory until the expectation is independently confirmed.
"""

import hashlib
import json
import time
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from workflow.models import requires_measured_evidence, source_hash
from workflow.validation import _same_input, _same_json
from workflow.wasi_runtime import execute_function


class Probe(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title: str = Field(min_length=1, max_length=160)
    contract_quote: str = Field(min_length=1, max_length=500)
    args: list[JsonValue] = Field(max_length=16)
    expected: JsonValue = None
    raises: Literal["ValueError", "TypeError", "KeyError", "IndexError", "ZeroDivisionError"] | None = None
    preserve_input: bool = False


class FocusedReview(BaseModel):
    model_config = ConfigDict(extra="forbid")
    summary: str = Field(max_length=500)
    requirements_status: Literal["met", "gap", "unknown"]
    requirements_reason: str = Field(min_length=1, max_length=600)
    probes: list[Probe] = Field(default_factory=list, max_length=6)


def enabled(data):
    # Old checkpoints with no strategy retain their original semantics.
    return (data.get("workflow_config", {}).get("review_strategy") == "focused"
            and data.get("validation_profile") == "functional_portable"
            and data.get("acceptance_id", "none") != "none" and data.get("mode") == "live")


def probe_id(probe):
    content = {k: probe.get(k) for k in ("args", "expected", "raises", "preserve_input")}
    return "P-" + hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def execute_probes(code, entrypoint, probes, timeout=10):
    digest = source_hash(code)
    if not probes:
        return {"code_hash": digest, "executed": False, "checks": [], "error": ""}
    result = execute_function(code, entrypoint, [{"id": p["id"], "args": p["args"]} for p in probes], timeout=timeout)
    rows = result.get("results", [])
    if (result.get("error") or not result.get("executed") or len(rows) != len(probes)
            or any(not isinstance(r, dict) for r in rows)
            or [r.get("id") for r in rows] != [p["id"] for p in probes]):
        return {"code_hash": digest, "executed": bool(result.get("executed")), "checks": [],
                "error": result.get("error") or "反例执行结果不完整。"}
    checks = []
    for p, row in zip(probes, rows):
        if (row.get("protocol_error") or not all(k in row for k in ("actual", "exception_types", "input_after"))
                or not isinstance(row["exception_types"], list)):
            return {"code_hash": digest, "executed": True, "checks": [], "error": "反例执行协议无效。"}
        exceptions = row["exception_types"]
        correct = ("builtins." + p["raises"] in exceptions) if p.get("raises") else (
            not exceptions and _same_json(row["actual"], p.get("expected")))
        preserved = not p.get("preserve_input") or _same_input(row["input_after"], p["args"])
        checks.append({"id": p["id"], "status": "passed" if correct and preserved else "failed",
                       "actual": row["actual"], "exceptions": exceptions, "input_preserved": preserved,
                       "expected": p.get("raises") or p.get("expected"), "args": p["args"],
                       "oracle": "model_authored", "contract_quote": p["contract_quote"]})
    return {"code_hash": digest, "executed": True, "checks": checks, "error": ""}


def review(data, code, settings, factory, started):
    from workflow.agents import TASK_AUTHORITY, close_model, role_model, structured

    contract = data["acceptance_spec"]
    previous = {p["id"]: p for r in data.get("review_history", []) for p in r.get("probes", [])}
    prior = next((r for r in reversed(data.get("review_history", [])) if r.get("strategy") == "focused"), {})
    replay = bool(prior.get("complete") and previous and not prior.get("pending_findings")
                  and prior.get("probe_plan_complete", prior.get("requirements_status") in {"met", "gap"}))
    if replay:
        # Freeze the reviewer's test plan; repeat execution, not paid speculation.
        result = FocusedReview(summary="重跑既有合同反例；本轮未重复调用审查模型。",
            requirements_status="met", requirements_reason="已核对范围限于冻结验收与保留的反例；不代表未覆盖行为已获证明。")
    else:
        model = role_model("review", settings, factory, data, started)
        try:
            result = structured(model, FocusedReview,
            "你是独立审查 Agent。一次完成需求核对与边界反例设计。" + TASK_AUTHORITY
            + "仅依据完整任务合同检查功能；不要提出重构、风格或额外需求。每个反例给出入口函数 args、"
            "预期 expected 或 raises，以及 task 或 acceptance_contract.task 中逐字引用的 contract_quote。"
            "选最多四个有区分力的边界，使用短输入和短预期（各不超过160字符），不要展开大段重复字符串。"
            "务必手工核对预期；程序将实际执行来核实，不相信你对代码运行结果的猜测。"
            "已有反例会自动重跑，不重复生成。即使代码看起来正确也可给边界探针。"
            "preserve_input 仅在合同明确要求输入不变时为 true。不输出源码或长推理。无证据的功能疑虑返回 unknown；"
            "静态分析不能证明性能或准确率提升。",
            {"task": data["task"], "acceptance_contract": {k: v for k, v in contract.items() if k != "task" or v != data["task"]}, "code": code,
             "existing_probes": list(previous.values())}, max_tokens=4096)
        finally:
            close_model(model)
    authoritative = data["task"] + "\n" + contract.get("task", "")
    rejected = []
    for item in result.probes:
        p = item.model_dump()
        if p["contract_quote"] not in authoritative or len(json.dumps(p, ensure_ascii=False)) > 6000:
            rejected.append({"title": p["title"], "severity": "medium", "review_note": "反例缺少逐字合同依据或超出大小限制。"})
            continue
        p["id"] = probe_id(p)
        previous.setdefault(p["id"], p)
    probes = list(previous.values())
    if len(probes) > 24:
        from workflow.agents import ContractError
        raise ContractError("累计反例超过 24 项；保留已有证据，停止扩大审查。")
    remaining = data["budget_limits"]["max_active_seconds"] - data["metrics"]["active_seconds"] - (time.monotonic() - started)
    execution = execute_probes(code, contract["entrypoint"], probes, timeout=max(.1, min(10, remaining)))
    by_id = {p["id"]: p for p in probes}
    findings = []
    for check in execution["checks"]:
        if check["status"] != "failed":
            continue
        p = by_id[check["id"]]
        evidence = json.dumps(check, ensure_ascii=False)
        findings.append({"id": p["id"], "title": p["title"], "severity": "medium", "verification": "model_probe",
                         "line_start": 1, "line_end": 1, "evidence": evidence,
                         "explanation": "隔离执行与模型依据合同提出的预期不一致；预期仍可能有误。",
                         "suggestion": "先核对合同与反例预期，仅修复有依据的差距。", "oracle": "model_authored"})
    requirement, reason = result.requirements_status, result.requirements_reason
    plan_complete = requirement in {"met", "gap"}
    if requires_measured_evidence(data["task"]):
        requirement, reason = "unknown", "性能或准确率目标仍需同条件前后实测。"
    elif findings:
        requirement, reason = "unknown", "模型反例与实际不一致，反例预期尚未经独立确认，需要人工核对。"
    elif requirement == "gap":
        requirement, reason = "unknown", "审查声称存在缺口，但没有可复现的失败反例；不据此修改源码。"
    return {"code_hash": source_hash(code), "strategy": "focused", "replayed": replay,
            "probe_plan_complete": plan_complete, "complete": not execution["error"],
            "summary": result.summary, "requirements_status": requirement, "requirements_reason": reason,
            "findings": findings, "pending_findings": rejected, "probes": probes, "probe_execution": execution,
            "report": {"strategy": "focused", "limitations": ["反例预期由模型提出，不能替代冻结验收。"]}}


def lost_passes(before, after):
    """Missing, skipped and errored checks must not silently count as preserved."""
    statuses = {c["id"]: c["status"] for c in after.get("checks", [])}
    return [c["id"] for c in before.get("checks", [])
            if c["status"] == "passed" and statuses.get(c["id"]) != "passed"]
