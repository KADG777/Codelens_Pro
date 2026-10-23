"""Repeatable fault injection; zero model calls, distinct from accuracy trials."""

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

from workflow import WorkflowRunService
from workflow.canvas import template_graph
from workflow.examples import ORDER_TASK
from workflow.validation import validate_code


def run_probe(db_path):
    attempts = []
    def unstable(code, digest, profile, contract, **kwargs):
        attempts.append(digest)
        if len(attempts) == 1:
            return {"status": "error", "code_hash": digest, "functional_executed": False,
                    "failed_count": 0, "passed_count": 0,
                    "checks": [{"id": "injected_failure", "status": "error", "detail": "Injected one-time validator outage"}]}
        return validate_code(code, digest, profile, contract, **kwargs)
    service = WorkflowRunService(db_path, validation_runner=unstable)
    run = service.create_run(ORDER_TASK, scenario="clean", workflow_config={"canvas": template_graph(True)})
    blocked = service.execute(run)
    service.close()
    service = WorkflowRunService(db_path, validation_runner=unstable)
    recovered = service.resume(run)
    service.close()
    completed_reviews_before = sum(n["node"] == "review" for n in blocked["node_runs"])
    completed_reviews_after = sum(n["node"] == "review" for n in recovered["node_runs"])
    checks = {"paused_on_failure": blocked["status"] == "blocked", "continued_after_process_reopen": recovered["status"] == "completed_static",
              "successful_review_not_repeated": completed_reviews_before == completed_reviews_after == 1,
              "same_source_retried": len(attempts) == 2 and len(set(attempts)) == 1,
              "no_model_api_calls": recovered["metrics"]["model_calls"] == 0}
    return {"kind": "fault_injection_recovery", "real_api_used": False, "created_at": datetime.now(timezone.utc).isoformat(),
            "checks": checks, "passed": all(checks.values()), "run_id": run, "canvas_hash": recovered["canvas_hash"],
            "before": {"status": blocked["status"], "completed_nodes": blocked["canvas_cursor"]["outcomes"]},
            "after": {"status": recovered["status"], "metrics": recovered["metrics"], "node_runs": recovered["node_runs"]}}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("evals/results/recovery-probes.json"))
    parser.add_argument("--db", type=Path, default=Path("reports/recovery-probes.sqlite3"))
    args = parser.parse_args()
    if args.output.exists():
        raise ValueError("选择新报告路径，不覆盖已有探测记录。")
    result = run_probe(args.db)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result["checks"]))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
