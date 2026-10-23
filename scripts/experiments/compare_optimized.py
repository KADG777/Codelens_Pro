"""Frozen optimization check: old-task diagnostics, then new-task paired trials."""

import argparse
import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from review_agent.config import Settings
from scripts.experiments.compare_agent_value import ARMS, run_collaboration, run_feedback, summarize
from scripts.experiments.compare_workflows import (
    ExperimentBudget,
    _audits,
    audit_status,
    load_suite,
    run_direct,
)
from workflow.storage import replace_report

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "evals/results/optimized-live.json"
LIMITS = {"max_model_calls": 8, "max_tokens": 16_000, "max_active_seconds": 240.0}


def run(output=OUTPUT, prior_path=None):
    if output.exists():
        raise ValueError("报告已存在，禁止覆盖或选择性补跑。")
    old, old_hash = load_suite(ROOT / "evals/agent_value_cases.json")
    suite, digest = load_suite(ROOT / "evals/optimized_cases.json")
    settings = replace(Settings.from_env(), max_retries=0)
    settings.validate()
    budget = ExperimentBudget(120, 200_000)
    prior = json.loads(prior_path.read_text(encoding="utf-8")) if prior_path else None
    if prior:
        if not prior.get("completed_at") or prior["suite_sha256"] != digest:
            raise ValueError("前轮实验不完整或冻结任务不匹配。")
        budget.calls = prior["budget_usage"]["calls"]
        budget.actual_tokens = prior["budget_usage"]["actual_tokens"]
        budget.charged_tokens = prior["budget_usage"]["charged_tokens"]
    report = {"schema_version": 1, "kind": "agent_value_comparison", "real_api_used": True,
        "created_at": datetime.now(timezone.utc).isoformat(), "suite_id": suite["suite_id"],
        "suite_sha256": digest, "diagnostic_suite_sha256": old_hash, "frozen_at": suite["frozen_at"],
        "model": settings.model, "temperature": .1, "repetitions": 3,
        "policy": {"max_calls": 120, "max_tokens": 200_000, "workflow": "focused; freeze and replay probes; pre-adoption regression guard",
            "workflow_limits": LIMITS, "direct_inferences": 1, "single_feedback_max_inferences": 3,
            "workflow_max_repairs": 2, "holdout": "only after all three candidates are frozen",
            "diagnostics": "four previously observed tasks, one workflow run each; not new-task evidence",
            "limitations": "four internally authored tasks; repeats are not independent tasks; model-authored probe oracles may be wrong"},
        "implementation_sha256": {str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [Path(__file__), ROOT / "scripts/experiments/compare_agent_value.py", ROOT / "scripts/experiments/compare_workflows.py", ROOT / "review_agent/llm.py", *sorted((ROOT / "workflow").glob("*.py"))]},
        "diagnostics": [] if prior else [{**c, "workflow": {}} for c in old["cases"]],
        "entries": [{**c, "case_id": c["id"], "id": f"{c['id']}-r{r+1}", "repeat": r+1,
            "initial": {"code": c.get("source")}, **{a: {} for a in ARMS}} for r in range(3) for c in suite["cases"]]}
    report["planned_case_ids"] = [e["id"] for e in report["entries"]]
    if prior:
        report["engineering_retest"] = {"prior_report": prior_path.name,
            "prior_sha256": hashlib.sha256(prior_path.read_bytes()).hexdigest(),
            "prior_budget_usage": prior["budget_usage"],
            "reason": "Specific format feedback, short contract quotes, compact output bounds and one budgeted transient transport retry; same tasks, not a new blind test. No cases or oracles changed."}

    def save():
        report["summary"] = summarize(report["entries"])
        report["budget_usage"] = {"calls": budget.calls, "actual_tokens": budget.actual_tokens, "charged_tokens": budget.charged_tokens}
        raw = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if settings.api_key and settings.api_key in raw:
            raise ValueError("拒绝保存含凭证的报告。")
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary = output.with_suffix(".json.tmp")
        temporary.write_text(raw, encoding="utf-8", newline="\n")
        replace_report(temporary, output)

    def workflow(case):
        return run_collaboration(case, settings, budget.factory, ROOT / "reports/optimized-live.sqlite3",
                                 review_strategy="focused", limits=LIMITS)

    save()
    for e in report["diagnostics"]:
        print("diagnostic " + e["id"], flush=True)
        try:
            e["workflow"] = workflow(e)
        except Exception as exc:
            e["workflow"] = {"status": "failed", "code": None, "error_type": type(exc).__name__}
        e["workflow"]["audits"] = _audits(e["workflow"].get("code"), e["task_id"])
        save()
    for index, e in enumerate(report["entries"]):
        offset = (index % 4 + e["repeat"] - 1) % 3
        order = ARMS[offset:] + ARMS[:offset]
        e["execution_order"] = list(order)
        for arm in order:
            print(f"{index+1}/12 {e['id']} {arm}", flush=True)
            try:
                e[arm] = workflow(e) if arm == "workflow" else (
                    run_feedback(e, settings, budget.factory) if arm == "single_feedback" else run_direct(e, settings, budget.factory))
            except Exception as exc:
                e[arm] = {"status": "failed", "code": None, "error_type": type(exc).__name__}
            save()
        for arm in ("initial", *ARMS):
            e[arm]["audits"] = _audits(e[arm].get("code"), e["task_id"])
        save()
        print(json.dumps({a: audit_status(e[a]) for a in ARMS}), flush=True)
    report["completed_at"] = datetime.now(timezone.utc).isoformat()
    save()
    print(json.dumps(report["summary"], ensure_ascii=False), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--retest", action="store_true", help="Complete same-task retest within the ORIGINAL shared authorization cap")
    args = parser.parse_args()
    run(ROOT / "evals/results/optimized-retest.json", OUTPUT) if args.retest else run()
