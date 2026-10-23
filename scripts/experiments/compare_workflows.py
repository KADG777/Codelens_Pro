"""Frozen, paired direct-model vs workflow experiment on synthetic tasks only."""

import argparse
import hashlib
import json
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from review_agent.config import Settings
from review_agent.llm import DeepSeekClient
from scripts.evaluation.evaluate_workflow import _audit
from workflow import WorkflowRunService
from workflow.agents import TASK_AUTHORITY, BudgetExceeded
from workflow.models import CodingOutput, normalize_source, source_hash
from workflow.registry import (
    FunctionCase,
    TaskSpec,
    get_acceptance_hash,
    get_task_spec,
    register_task_spec,
)
from workflow.storage import replace_report
from workflow.validation import audit_execute

ROOT = Path(__file__).resolve().parents[2]
ARMS = ("direct", "workflow")


def load_suite(path):
    raw = Path(path).read_bytes()
    suite = json.loads(raw)
    if suite.get("schema_version") != 1 or not suite.get("cases"):
        raise ValueError("Invalid paired suite")
    if len({c["id"] for c in suite["cases"]}) != len(suite["cases"]):
        raise ValueError("Duplicate case IDs")
    for entry in suite.get("fresh_specs", []):
        def cases(key):
            return tuple(FunctionCase(**{**c, "args": tuple(c["args"])}) for c in entry[key])
        spec = TaskSpec(entry["id"], entry["label"], entry["task"], entry["entrypoint"], tuple(entry["parameters"]),
                        cases("public_cases"), cases("holdout_cases"), reference_code="")
        register_task_spec(spec)
    for case in suite["cases"]:
        spec = get_task_spec(case["task_id"])
        if spec.task != case["task"] or case.get("contract_hash", get_acceptance_hash(spec.id)) != get_acceptance_hash(spec.id):
            raise ValueError("Frozen contract changed")
    return suite, hashlib.sha256(raw).hexdigest()


class ExperimentBudget:
    """Shared across both arms. Reserve UTF-8 bytes + output cap before requests.

    The byte bound is conservative for ordinary byte-tokenized input. Unreported
    usage is charged the reservation; actual provider usage is kept separately.
    """
    def __init__(self, max_calls=96, max_tokens=220_000, client_factory=DeepSeekClient):
        self.max_calls, self.max_tokens = max_calls, max_tokens
        self.calls = self.charged_tokens = self.actual_tokens = 0
        self.client_factory = client_factory
        self.lock = threading.Lock()
        self.reserved_tokens = 0

    def factory(self, role, settings):
        budget = self
        inner = self.client_factory(settings)

        class Client:
            metrics = inner.metrics
            client = getattr(inner, "client", None)

            def complete(self, messages, *, tools=None, json_output=False, max_tokens=6000):
                estimate = len(json.dumps({"messages": messages, "tools": tools}, ensure_ascii=False).encode("utf-8")) + 1024
                with budget.lock:
                    available = budget.max_tokens - budget.charged_tokens - budget.reserved_tokens
                    if budget.calls >= budget.max_calls or available - estimate < 256:
                        raise BudgetExceeded("同任务对照实验总预算耗尽；保留未执行项，不补跑挑选结果。")
                    cap = min(max_tokens, available - estimate)
                    reservation = estimate + cap
                    budget.reserved_tokens += reservation
                    budget.calls += 1
                before = self.metrics.prompt_tokens + self.metrics.completion_tokens
                try:
                    return inner.complete(messages, tools=tools, json_output=json_output, max_tokens=cap)
                finally:
                    actual = max(0, self.metrics.prompt_tokens + self.metrics.completion_tokens - before)
                    with budget.lock:
                        budget.reserved_tokens -= reservation
                        budget.actual_tokens += actual
                        budget.charged_tokens += actual if actual else reservation

            def close(self):
                inner.close()

        result = Client()
        result.settings = settings
        return result


def direct_messages(case):
    return [{"role": "system", "content": "按任务直接生成或修复完整 Python 单文件源码，仅用标准库，保留公开接口。"
             + TASK_AUTHORITY + "仅返回 JSON：" + json.dumps(CodingOutput.model_json_schema(), ensure_ascii=False)},
            {"role": "user", "content": json.dumps({"task": case["task"], "code": case.get("source"),
             "acceptance_contract": get_task_spec(case["task_id"]).model_contract()}, ensure_ascii=False)}]


def run_direct(case, settings, factory):
    model = factory("direct", settings)
    started = time.perf_counter()
    code, status, error = None, "completed", ""
    try:
        # Exactly one inference. No hidden format retry, review, tools, or test feedback.
        response = model.complete(direct_messages(case), json_output=True, max_tokens=8192)
        if response.get("finish_reason") == "length":
            raise ValueError("truncated output")
        code = normalize_source(CodingOutput.model_validate_json(response.get("content") or "").code, "solution.py")
    except Exception as exc:
        status, error = "failed", type(exc).__name__
    finally:
        model.close()
    return {"status": status, "error_type": error, "code": code,
            "elapsed_seconds": round(time.perf_counter()-started, 6),
            "metrics": {k: getattr(model.metrics, k, 0) for k in ("model_calls", "http_attempts", "prompt_tokens", "completion_tokens")}}


def run_workflow(case, settings, factory, db_path):
    started = time.perf_counter()
    with_service = WorkflowRunService(db_path, model_factory=factory)
    run_id = with_service.create_run(case["task"], source=case.get("source"), mode="live", acceptance_id=case["task_id"],
        validation_profile="functional_portable", workflow_config={"parallel_checks": True, "extension_nodes": ["acceptance_explainer"], "review_strategy": "full"},
        budget_limits={"max_model_calls": 18, "max_tokens": 40_000, "max_active_seconds": 240.0})
    try:
        result = with_service.execute(run_id, settings=settings)
    finally:
        with_service.close()
    item = next((v for v in result["versions"] if v["id"] == result["current_version"]), {})
    return {"run_id": run_id, "status": result["status"], "termination_reason": result["termination_reason"],
            "code": item.get("code"), "elapsed_seconds": round(time.perf_counter()-started, 6), "metrics": result["metrics"],
            "roles_executed": sorted({r["node"] for r in result.get("node_runs", [])}),
            "versions": len(result["versions"]), "fix_rounds": result["fix_round"]}


def audit_status(result: dict) -> str:
    code = result.get("code")
    audits = result.get("audits", {})
    if code is None or set(audits) != {"public", "holdout"}:
        return "unavailable"
    all_checks = []
    for audit in audits.values():
        if audit.get("code_hash") != source_hash(code) or not audit.get("executed") or not audit.get("checks"):
            return "unavailable"
        checks = audit["checks"]
        if any(c.get("status") not in {"passed", "failed"} for c in checks):
            return "unavailable"
        all_checks.extend(checks)
    return "passed" if all(c["status"] == "passed" for c in all_checks) else "failed"


def summarize_pairs(entries):
    summary = {}
    for split in ("all", "development", "fresh"):
        selected = entries if split == "all" else [e for e in entries if e["split"] == split]
        summary[split] = {}
        for arm in ARMS:
            values = [e.get(arm, {}) for e in selected]
            outcomes = [audit_status(v) for v in values]
            regressions = 0
            lost_checks = 0
            for entry, value in zip(selected, values):
                initial = entry.get("initial", {})
                if audit_status(initial) == "passed" and audit_status(value) == "failed":
                    regressions += 1
                if audit_status(initial) != "unavailable" and audit_status(value) != "unavailable":
                    before = {(part, c["id"]) for part, a in initial["audits"].items() for c in a["checks"] if c["status"] == "passed"}
                    lost_checks += sum((part,c["id"]) in before and c["status"] == "failed"
                                      for part,a in value["audits"].items() for c in a["checks"])
            summary[split][arm] = {"planned": len(selected), "passed": outcomes.count("passed"),
                "failed": outcomes.count("failed"), "unavailable": outcomes.count("unavailable"),
                "pass_rate": outcomes.count("passed") / len(selected) if selected else None,
                "regressions": regressions, "regressed_checks": lost_checks,
                "seconds": round(sum(v.get("elapsed_seconds", 0) for v in values), 4),
                "tokens": sum(v.get("metrics", {}).get("prompt_tokens", 0)+v.get("metrics", {}).get("completion_tokens", 0) for v in values),
                "calls": sum(v.get("metrics", {}).get("model_calls", 0) for v in values)}
        summary[split]["workflow_only_passes"] = sum(audit_status(e.get("workflow",{})) == "passed" and audit_status(e.get("direct",{})) == "failed" for e in selected)
        summary[split]["direct_only_passes"] = sum(audit_status(e.get("direct",{})) == "passed" and audit_status(e.get("workflow",{})) == "failed" for e in selected)
    return summary


def _audits(code, task_id):
    if code is None:
        return {}
    result = {}
    for holdout, key in ((False, "public"), (True, "holdout")):
        def executor(source, contract, **kwargs):
            return audit_execute(source, contract, holdout=holdout, timeout=kwargs["timeout"])
        result[key] = _audit(code, task_id, executor, 10)
    return result


def run_experiment(suite_path, output, db_path, settings, max_calls=96, max_tokens=220_000):
    suite, digest = load_suite(suite_path)
    if Path(output).exists():
        raise ValueError("Report already exists; use a new path, never overwrite a trial")
    budget = ExperimentBudget(max_calls, max_tokens)
    report = {"schema_version": 1, "kind": "paired_comparison", "real_api_used": True,
        "created_at": datetime.now(timezone.utc).isoformat(), "suite_id": suite["suite_id"], "suite_sha256": digest,
        "frozen_at": suite["frozen_at"], "model": settings.model, "temperature": .1, "repetitions": 1,
        "policy": {"max_calls": max_calls, "max_tokens": max_tokens, "direct_inferences": 1,
                   "workflow_max_calls_per_task": 18, "parallel_checks": True, "extension_nodes": ["acceptance_explainer"],
                   "timing": "arm wall time, includes internal workflow checks, excludes independent post-run audits",
                   "holdout": "executed only after both arms finish; never fed back to models"},
        "planned_case_ids": [c["id"] for c in suite["cases"]], "entries": []}
    def save():
        report["summary"] = summarize_pairs(report["entries"])
        report["budget_usage"] = {"calls": budget.calls, "actual_tokens": budget.actual_tokens, "charged_tokens": budget.charged_tokens}
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(str(output)+".tmp")
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
        replace_report(temporary, Path(output))
    # Include every planned pair before running any of them.
    report["entries"] = [{**case, "direct": {}, "workflow": {}, "initial": {"code": case.get("source")}} for case in suite["cases"]]
    save()
    for index, entry in enumerate(report["entries"]):
        order = ARMS if index % 2 == 0 else tuple(reversed(ARMS))
        entry["execution_order"] = list(order)
        for arm in order:
            print(f"{index+1}/{len(report['entries'])} {entry['id']} {arm}", flush=True)
            try:
                entry[arm] = run_direct(entry, settings, budget.factory) if arm == "direct" else run_workflow(entry, settings, budget.factory, db_path)
            except Exception as exc:
                entry[arm] = {"status": "failed", "error_type": type(exc).__name__, "code": None}
            save()
        # Independent grading is delayed until both candidates are fixed.
        for stage in ("initial", *ARMS):
            entry[stage]["audits"] = _audits(entry[stage].get("code"), entry["task_id"])
        save()
        print(json.dumps({"direct": audit_status(entry["direct"]), "workflow": audit_status(entry["workflow"]),
                          "calls": budget.calls, "tokens": budget.actual_tokens}), flush=True)
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    save()
    return report


def main():
    parser = argparse.ArgumentParser(description="Same-task direct-model vs multi-Agent experiment")
    parser.add_argument("--suite", type=Path, default=ROOT/"evals/paired_cases.json")
    parser.add_argument("--output", type=Path, default=ROOT/"evals/results/paired-live.json")
    parser.add_argument("--db", type=Path, default=ROOT/"reports/paired-live.sqlite3")
    parser.add_argument("--max-calls", type=int, default=96)
    parser.add_argument("--max-tokens", type=int, default=220_000)
    args = parser.parse_args()
    settings = replace(Settings.from_env(), max_retries=0)
    settings.validate()
    report = run_experiment(args.suite, args.output, args.db, settings, args.max_calls, args.max_tokens)
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
