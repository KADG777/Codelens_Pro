"""One separately reported recovery probe; primary comparison stays immutable."""

import hashlib
import json
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from compare_workflows import ExperimentBudget, _audits, audit_status, load_suite
from review_agent.config import Settings
from workflow import WorkflowRunService

ROOT = Path(__file__).resolve().parent


def main():
    source = ROOT / "evals/results/agent-value-live.json"
    output = ROOT / "evals/results/real-recovery.json"
    if output.exists():
        raise ValueError("真实恢复报告已存在，不覆盖或重复调用。")
    raw = source.read_bytes()
    primary = json.loads(raw)
    suite_path = ROOT / "evals/agent_value_cases.json"
    _, digest = load_suite(suite_path)
    if digest != primary["suite_sha256"] or not primary.get("completed_at"):
        raise ValueError("只允许恢复已完成、任务集未变的主实验。")
    entry = next(e for e in primary["entries"] if e.get("workflow", {}).get("status") == "blocked")
    usage = primary["budget_usage"]
    remaining_calls = 180 - usage["calls"]
    remaining_tokens = 500_000 - usage["charged_tokens"]
    if remaining_calls <= 0 or remaining_tokens <= 0:
        raise ValueError("无剩余授权预算。")
    budget = ExperimentBudget(remaining_calls, remaining_tokens)
    settings = replace(Settings.from_env(), max_retries=0)
    settings.validate()
    if settings.base_url != "https://api.deepseek.com":
        raise ValueError("本次授权仅允许 DeepSeek 官方接口。")
    report = {"kind": "real_api_recovery", "real_api_used": True, "primary_sha256": hashlib.sha256(raw).hexdigest(),
              "primary_score_unchanged": True, "case_id": entry["id"], "run_id": entry["workflow"]["run_id"],
              "created_at": datetime.now(timezone.utc).isoformat(), "extra_budget": {"calls": remaining_calls, "tokens": remaining_tokens}}
    def save():
        text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
        if settings.api_key and settings.api_key in text:
            raise ValueError("拒绝保存包含凭证的报告。")
        output.write_text(text, encoding="utf-8")
    save()
    service = WorkflowRunService(ROOT / "reports/agent-value-live.sqlite3", model_factory=budget.factory)
    try:
        before = service.get_snapshot(report["run_id"])
        if before["status"] != "blocked":
            raise ValueError("运行已不处于原阻塞状态，拒绝重复恢复。")
        after = service.resume(report["run_id"], settings=settings)
    finally:
        service.close()
    code = next(v["code"] for v in after["versions"] if v["id"] == after["current_version"])
    candidate = {"code": code, "audits": _audits(code, entry["task_id"])}
    old_ids = [n["id"] for n in before["node_runs"]]
    new_nodes = after["node_runs"][len(old_ids):]
    report.update(completed_at=datetime.now(timezone.utc).isoformat(), before_status=before["status"], after_status=after["status"],
        resumed_node=new_nodes[0]["node"] if new_nodes else None,
        preserved_prior_records=after["node_runs"][:len(old_ids)] == before["node_runs"],
        candidate=candidate, independent_outcome=audit_status(candidate), new_node_runs=new_nodes,
        extra_usage={"calls": budget.calls, "actual_tokens": budget.actual_tokens, "charged_tokens": budget.charged_tokens},
        combined_usage={"calls": usage["calls"] + budget.calls, "actual_tokens": usage["actual_tokens"] + budget.actual_tokens,
                        "charged_tokens": usage["charged_tokens"] + budget.charged_tokens})
    save()
    print(json.dumps({k: report[k] for k in ("before_status", "after_status", "independent_outcome", "preserved_prior_records", "combined_usage")}))


if __name__ == "__main__":
    main()
