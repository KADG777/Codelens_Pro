"""Preregistered three-arm repeated experiment; never overwrite an existing trial."""

import argparse
import hashlib
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from compare_workflows import (
    ExperimentBudget,
    _audits,
    audit_status,
    direct_messages,
    load_suite,
    run_direct,
)
from review_agent.config import Settings
from workflow import WorkflowRunService
from workflow.models import CodingOutput, normalize_source, source_hash
from workflow.storage import replace_report
from workflow.validation import validate_code

ROOT = Path(__file__).resolve().parent
ARMS = ("direct", "single_feedback", "workflow")
PER_ARM_CALLS, PER_ARM_TOKENS, PER_ARM_SECONDS = 18, 40_000, 240.0


def run_feedback(case, settings, factory, validator=validate_code):
    model = factory("single_feedback", settings)
    started = time.perf_counter()
    messages = direct_messages(case)
    code, status, error, checks = None, "completed", "", []
    try:
        for attempt in range(3):
            used = model.metrics.prompt_tokens + model.metrics.completion_tokens
            estimate = len(json.dumps(messages, ensure_ascii=False).encode()) + 1024
            cap = min(8192, PER_ARM_TOKENS - used - estimate)
            if cap < 256 or time.perf_counter() - started >= PER_ARM_SECONDS:
                status = "budget_exhausted"
                break
            response = model.complete(messages, json_output=True, max_tokens=cap)
            if response.get("finish_reason") == "length":
                raise ValueError("truncated output")
            code = normalize_source(CodingOutput.model_validate_json(response.get("content") or "").code, "solution.py")
            check = validator(code, source_hash(code), "functional_portable", case["task_id"], timeout=10)
            checks.append({"attempt": attempt + 1, "code_hash": source_hash(code), "validation": check})
            if check["status"] == "passed" and check.get("functional_executed"):
                break
            if check["status"] == "error":
                status = "validation_error"
                break
            messages.extend([{"role": "assistant", "content": response["content"]},
                             {"role": "user", "content": "请根据独立公开验收结果修复完整源码，保留同一 JSON 格式：" + json.dumps(check, ensure_ascii=False)}])
    except Exception as exc:
        status, error = "failed", type(exc).__name__
    finally:
        model.close()
    return {"status": status, "error_type": error, "code": code, "feedback_history": checks,
            "elapsed_seconds": round(time.perf_counter() - started, 6),
            "metrics": {k: getattr(model.metrics, k, 0) for k in ("model_calls", "http_attempts", "prompt_tokens", "completion_tokens")}}


def run_collaboration(case, settings, factory, db_path):
    started = time.perf_counter()
    service = WorkflowRunService(db_path, model_factory=factory)
    try:
        run_id = service.create_run(case["task"], source=case.get("source"), mode="live", acceptance_id=case["task_id"],
            validation_profile="functional_portable", workflow_config={"parallel_checks": True},
            budget_limits={"max_model_calls": PER_ARM_CALLS, "max_tokens": PER_ARM_TOKENS, "max_active_seconds": PER_ARM_SECONDS})
        data = service.execute(run_id, settings=settings)
    finally:
        service.close()
    artifact = next((v for v in data["versions"] if v["id"] == data["current_version"]), {})
    return {"run_id": run_id, "status": data["status"], "termination_reason": data["termination_reason"],
            "code": artifact.get("code"), "metrics": data["metrics"],
            "elapsed_seconds": round(time.perf_counter() - started, 6), "fix_rounds": data["fix_round"],
            "trace": {"nodes": data["node_runs"], "versions": [{k: v[k] for k in ("id", "hash", "parent_id") if k in v} for v in data["versions"]],
                      "review_history": data["review_history"], "validation_history": data["validation_history"],
                      "repair_records": [{k: v for k, v in r.items() if k != "revised_code"} for r in data["repair_records"]]}}


def summarize(entries):
    """Report paired trials and per-task repeats, without treating repeats as new tasks."""
    result = {"arms": {}, "paired": {}, "tasks": {}}
    for arm in ARMS:
        values = [e.get(arm, {}) for e in entries]
        outcomes = [audit_status(v) for v in values]
        tokens = sum(v.get("metrics", {}).get("prompt_tokens", 0) + v.get("metrics", {}).get("completion_tokens", 0) for v in values)
        seconds = sum(v.get("elapsed_seconds", 0) for v in values)
        regressions = 0
        for entry, value in zip(entries, values):
            if audit_status(entry.get("initial", {})) == "unavailable" or audit_status(value) == "unavailable":
                continue
            before = {(p, c["id"]) for p, a in entry["initial"]["audits"].items() for c in a["checks"] if c["status"] == "passed"}
            regressions += sum((p, c["id"]) in before and c["status"] == "failed" for p, a in value["audits"].items() for c in a["checks"])
        result["arms"][arm] = {"planned": len(entries), "passed": outcomes.count("passed"), "failed": outcomes.count("failed"),
            "unavailable": outcomes.count("unavailable"), "tokens": tokens, "seconds": round(seconds, 4),
            "calls": sum(v.get("metrics", {}).get("model_calls", 0) for v in values), "regressed_checks": regressions,
            "tokens_per_success": round(tokens / outcomes.count("passed"), 2) if "passed" in outcomes else None}
    for baseline in ARMS[:-1]:
        result["paired"][baseline] = {"workflow_only_pass": sum(audit_status(e.get("workflow", {})) == "passed" and audit_status(e.get(baseline, {})) == "failed" for e in entries),
                                     "baseline_only_pass": sum(audit_status(e.get(baseline, {})) == "passed" and audit_status(e.get("workflow", {})) == "failed" for e in entries)}
    for case_id in dict.fromkeys(e["case_id"] for e in entries):
        rows = [e for e in entries if e["case_id"] == case_id]
        result["tasks"][case_id] = {a: [audit_status(e.get(a, {})) for e in rows] for a in ARMS}
    result["trace"] = {"workflow_trials_with_bound_handoffs": sum(bool(e.get("workflow", {}).get("trace", {}).get("nodes")) and
        all(n.get("input", {}).get("code_hash") and n.get("output", {}).get("code_hash") for n in e["workflow"]["trace"]["nodes"] if n["node"] != "coding") for e in entries),
        "workflow_repair_trials": sum(e.get("workflow", {}).get("fix_rounds", 0) > 0 for e in entries)}
    return result


def run_experiment(suite_path, output, db_path, settings, max_calls=180, max_tokens=500_000):
    suite, digest = load_suite(suite_path)
    if Path(output).exists():
        raise ValueError("报告已存在；不得覆盖或根据结果补跑。")
    budget = ExperimentBudget(max_calls, max_tokens)
    report = {"schema_version": 1, "kind": "agent_value_comparison", "real_api_used": True,
        "created_at": datetime.now(timezone.utc).isoformat(), "suite_id": suite["suite_id"], "suite_sha256": digest,
        "frozen_at": suite["frozen_at"], "model": settings.model, "temperature": .1, "repetitions": 3,
        "policy": {"max_calls": max_calls, "max_tokens": max_tokens, "per_arm_call_cap": PER_ARM_CALLS,
            "per_arm_token_cap": PER_ARM_TOKENS, "direct_inferences": 1, "single_feedback_max_inferences": 3,
            "workflow_max_repairs": 2, "workflow_extensions": [], "holdout": "only after all three candidates are frozen",
            "timing": "arm wall time including internal checks; excludes final common grading",
            "limitations": "4 internally authored synthetic tasks; repeats are not independent tasks; no universal superiority claim"},
        "implementation_sha256": {str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
                                  for p in [Path(__file__).resolve(), ROOT / "compare_workflows.py", *sorted((ROOT / "workflow").glob("*.py"))]},
        "entries": [{**case, "case_id": case["id"], "id": f"{case['id']}-r{repeat+1}", "repeat": repeat+1,
                     "initial": {"code": case.get("source")}, **{a: {} for a in ARMS}}
                    for repeat in range(3) for case in suite["cases"]]}
    report["planned_case_ids"] = [e["id"] for e in report["entries"]]
    def save():
        report["summary"] = summarize(report["entries"])
        report["budget_usage"] = {"calls": budget.calls, "actual_tokens": budget.actual_tokens, "charged_tokens": budget.charged_tokens}
        raw = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if settings.api_key and settings.api_key in raw:
            raise ValueError("拒绝保存：报告包含凭证。")
        Path(output).parent.mkdir(parents=True, exist_ok=True)
        temp = Path(str(output) + ".tmp")
        temp.write_text(raw, encoding="utf-8")
        replace_report(temp, Path(output))
    save()
    for i, entry in enumerate(report["entries"]):
        offset = (suite["cases"].index(next(c for c in suite["cases"] if c["id"] == entry["case_id"])) + entry["repeat"] - 1) % 3
        order = ARMS[offset:] + ARMS[:offset]
        entry["execution_order"] = list(order)
        for arm in order:
            print(f"{i+1}/12 {entry['id']} {arm}", flush=True)
            try:
                entry[arm] = (run_collaboration(entry, settings, budget.factory, db_path) if arm == "workflow" else
                              run_feedback(entry, settings, budget.factory) if arm == "single_feedback" else run_direct(entry, settings, budget.factory))
            except Exception as exc:
                entry[arm] = {"status": "failed", "code": None, "error_type": type(exc).__name__}
            save()
        for arm in ("initial", *ARMS):
            entry[arm]["audits"] = _audits(entry[arm].get("code"), entry["task_id"])
        save()
        print(json.dumps({a: audit_status(entry[a]) for a in ARMS}), flush=True)
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    save()
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--suite", type=Path, default=ROOT / "evals/agent_value_cases.json")
    parser.add_argument("--output", type=Path, default=ROOT / "evals/results/agent-value-live.json")
    parser.add_argument("--db", type=Path, default=ROOT / "reports/agent-value-live.sqlite3")
    parser.add_argument("--max-calls", type=int, default=180)
    parser.add_argument("--max-tokens", type=int, default=500_000)
    args = parser.parse_args()
    settings = replace(Settings.from_env(), max_retries=0)
    settings.validate()
    if settings.base_url != "https://api.deepseek.com":
        raise ValueError("本次授权仅允许 DeepSeek 官方接口。")
    print(json.dumps(run_experiment(args.suite, args.output, args.db, settings, args.max_calls, args.max_tokens)["summary"], ensure_ascii=False))


if __name__ == "__main__":
    main()
