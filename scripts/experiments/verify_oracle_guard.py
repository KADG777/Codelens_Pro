"""Targeted post-hoc check; never replaces either full comparison's score."""

import copy
import hashlib
import json
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from review_agent.config import Settings
from scripts.experiments.compare_agent_value import run_collaboration
from scripts.experiments.compare_workflows import (
    ExperimentBudget,
    _audits,
    audit_status,
    load_suite,
)
from workflow.models import FixOutput, current_artifact
from workflow.service import WorkflowRunService

ROOT = Path(__file__).resolve().parents[2]
OUTPUT = ROOT / "evals/results/oracle-guard-check.json"


def run():
    if OUTPUT.exists():
        raise ValueError("不得覆盖定点验证记录。")
    suite, suite_hash = load_suite(ROOT / "evals/optimized_cases.json")
    primary_path = ROOT / "evals/results/optimized-retest.json"
    raw = primary_path.read_bytes()
    primary = json.loads(raw)
    if not primary.get("completed_at") or primary["suite_sha256"] != suite_hash:
        raise ValueError("前轮实验未完成或任务集改变。")
    selected = [e for e in primary["entries"] if e["workflow"]["termination_reason"] == "candidate_rejected"]
    if len(selected) != 1:
        raise ValueError("需要唯一的候选拒绝案例；不选择性筛选。")
    entry = selected[0]
    service = WorkflowRunService(ROOT / "reports/optimized-live.sqlite3")
    try:
        before = service.get_snapshot(entry["workflow"]["run_id"])
        saved = copy.deepcopy(before)
        record = before["repair_records"][-1]
        response = FixOutput.model_validate({k: record[k] for k in FixOutput.model_fields if k in record})
        replay = copy.deepcopy(before)
        replay.update(status="running", error="", termination_reason="")
        with patch("workflow.agents.fix", return_value=response):
            service._perform("fix", replay, None, time.monotonic())
        artifact = current_artifact(replay)
        audits = _audits(artifact["code"], entry["task_id"])
        deterministic = {"adopted": replay["repair_records"][-1].get("adopted"),
            "candidate_hash": artifact["hash"], "code": artifact["code"], "audits": audits,
            "source_record_preserved": service.get_snapshot(entry["workflow"]["run_id"]) == saved,
            "record": {k: v for k, v in replay["repair_records"][-1].items() if k != "revised_code"},
            "extra_model_calls": 0}
    finally:
        service.close()
    settings = replace(Settings.from_env(), max_retries=0)
    budget = ExperimentBudget(140, 200_000)
    for field, key in (("calls", "calls"), ("actual_tokens", "actual_tokens"), ("charged_tokens", "charged_tokens")):
        setattr(budget, field, primary["budget_usage"][key])
    report = {"kind": "oracle_guard_check", "created_at": datetime.now(timezone.utc).isoformat(),
        "primary_sha256": hashlib.sha256(raw).hexdigest(), "suite_sha256": suite_hash,
        "case_id": entry["id"], "selection": "unique candidate_rejected run; no task or oracle changes",
        "policy": "post-hoc deterministic replay plus one live workflow; not a replacement full comparison",
        "authorized_max_calls": 140, "authorized_max_tokens": 200_000,
        "prior_budget_usage": primary["budget_usage"], "deterministic_replay": deterministic,
        "implementation_sha256": {str(p.relative_to(ROOT)).replace("\\", "/"): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in [Path(__file__), ROOT / "review_agent/llm.py", *sorted((ROOT / "workflow").glob("*.py"))]}}
    case = next(c for c in suite["cases"] if c["task_id"] == entry["task_id"])
    try:
        live = run_collaboration(case, settings, budget.factory, ROOT / "reports/oracle-guard-live.sqlite3",
            review_strategy="focused", limits={"max_model_calls": 5, "max_tokens": 9000, "max_active_seconds": 240.0})
    except Exception as exc:
        live = {"status": "failed", "code": None, "error_type": type(exc).__name__}
    live["audits"] = _audits(live.get("code"), case["task_id"])
    report.update(live=live, live_outcome=audit_status(live),
        completed_at=datetime.now(timezone.utc).isoformat(),
        combined_usage={"calls": budget.calls, "actual_tokens": budget.actual_tokens, "charged_tokens": budget.charged_tokens})
    encoded = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if settings.api_key and settings.api_key in encoded:
        raise ValueError("拒绝保存含凭证的报告。")
    OUTPUT.write_text(encoded, encoding="utf-8", newline="\n")
    print(json.dumps({"replay_adopted": deterministic["adopted"], "replay_outcome": audit_status(deterministic),
        "live_outcome": report["live_outcome"], "live_status": live["status"], "combined_usage": report["combined_usage"]}))


if __name__ == "__main__":
    run()
