"""Evaluate public synthetic workflows against post-run, independent holdout checks.

No candidate is executed by this module. An explicit sandbox audit adapter owns
execution. Offline smoke uses scripted models and is never labelled a live result.
"""

import argparse
import hashlib
import importlib
import json
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from review_agent.config import Settings
from review_agent.models import Metrics
from workflow import WorkflowRunService
from workflow.models import source_hash
from workflow.registry import build_harness, get_task_spec
from workflow.storage import replace_report

ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CASES = ROOT / "evals" / "workflow_cases.json"
EVALUATION_MODES = ("offline_smoke", "real_live")


class EvaluationCase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^[a-z0-9_]+$")
    task_id: str
    input_mode: Literal["generation", "upload"]
    expectation: Literal["generated", "repair", "clean_control", "safe_stop"]
    source: str | None = None
    source_kind: Literal["explicit", "reference"] = "explicit"
    smoke_initial_source: str | None = None
    task_suffix: str = ""
    validation_profile: Literal["functional_required", "functional_portable", "static_only"] = (
        "functional_portable"
    )
    max_fix_rounds: int = Field(default=2, ge=0, le=2)

    @model_validator(mode="after")
    def validate_source(self):
        if self.input_mode == "generation" and self.source is not None:
            raise ValueError("Generation cases cannot carry uploaded source")
        if self.input_mode == "upload" and self.source_kind == "explicit" and not self.source:
            raise ValueError("Uploaded cases require source or source_kind=reference")
        return self


def load_cases(path=DEFAULT_CASES) -> list[EvaluationCase]:
    suite = json.loads(Path(path).read_text(encoding="utf-8"))
    if suite.get("schema_version") != 1 or not isinstance(suite.get("cases"), list):
        raise ValueError("Unsupported evaluation case schema")
    cases = [EvaluationCase.model_validate(case) for case in suite["cases"]]
    if not cases or len({case.id for case in cases}) != len(cases):
        raise ValueError("Evaluation case IDs must be nonempty and unique")
    for case in cases:
        get_task_spec(case.task_id)
    return cases


def _resolve_case(case):
    spec = get_task_spec(case.task_id)
    source = None
    if case.input_mode == "upload":
        source = spec.reference_code if case.source_kind == "reference" else case.source
    task = spec.task + ("\n" + case.task_suffix if case.task_suffix else "")
    return spec, source, task


class _SmokeModel:
    """Labelled fixture responses; reference code is never used by real_live."""

    def __init__(self, role, case):
        self.role, self.case = role, case
        self.metrics = Metrics()

    def complete(self, messages, **kwargs):
        self.metrics.model_calls += 1
        self.metrics.prompt_tokens += 50
        self.metrics.completion_tokens += 30
        spec, _, _ = _resolve_case(self.case)
        payload = json.loads(messages[1]["content"])
        if self.role == "coding":
            code = self.case.smoke_initial_source or spec.buggy_code or spec.reference_code
            answer = {"code": code, "summary": "Offline smoke: fixed fixture response"}
        elif self.role == "fix":
            answer = {
                "base_code_hash": payload["base_code_hash"],
                "revised_code": spec.reference_code,
                "addressed_finding_ids": [],
                "change_summary": "Offline smoke: reference fixture, not a model result",
                "unresolved_items": [],
            }
        elif "candidates" in payload:
            answer = {
                "decisions": [
                    {
                        "finding_id": item["finding_id"],
                        "verdict": "supported",
                        "reason": "Offline smoke scripted verdict",
                        "basis": "local_code",
                        "impact_kind": "wrong_result",
                    }
                    for item in payload["candidates"]
                ]
            }
        elif "任务核对阶段" in messages[0]["content"]:
            answer = {
                "status": "unknown" if self.case.expectation == "safe_stop" else "met",
                "reason": "Offline smoke: scripted requirement verdict",
            }
        else:
            numbered = payload.get("numbered_source", "")
            code = "\n".join(line.split(": ", 1)[-1] for line in numbered.splitlines())
            bad_fixture = code.strip() != spec.reference_code.strip()
            lines = code.splitlines()
            index = next(
                (
                    i
                    for i, line in enumerate(lines, 1)
                    if line.strip()
                    and not line.lstrip().startswith(("#", "def ", "import ", "from "))
                ),
                1,
            )
            findings = []
            if bad_fixture and self.case.expectation != "safe_stop":
                findings = [
                    {
                        "title": "Offline smoke fixture needs repair",
                        "severity": "medium",
                        "category": "Bug",
                        "line_start": index,
                        "line_end": index,
                        "explanation": "A scripted fixture exercises the repair branch.",
                        "suggestion": "Implement the explicit public task contract.",
                        "evidence": lines[index - 1] if lines else code,
                    }
                ]
            answer = {
                "summary": "Offline smoke: no real model reasoning",
                "findings": findings,
                "limitations": ["Scripted smoke output is not model-quality evidence."],
            }
        return {"role": "assistant", "content": json.dumps(answer, ensure_ascii=False)}


def _unavailable_audit(code, task_id, reason):
    return {
        "code_hash": source_hash(code) if code is not None else None,
        "acceptance_id": task_id,
        "status": "not_executed",
        "executed": False,
        "checks": [],
        "passed_count": 0,
        "failed_count": 0,
        "error": reason,
    }


def _audit(code, task_id, executor, timeout):
    if code is None:
        return _unavailable_audit(None, task_id, "No committed candidate source")
    if executor is None:
        return _unavailable_audit(code, task_id, "No sandbox audit executor was provided")
    started = time.perf_counter()
    try:
        raw = executor(code, task_id, holdout=True, timeout=timeout)
        if not isinstance(raw, dict) or type(raw.get("executed")) is not bool:
            raise ValueError("Sandbox audit must return an explicit executed boolean")
        checks = raw.get("checks", [])
        if not isinstance(checks, list) or any(not isinstance(check, dict) for check in checks):
            raise ValueError("Sandbox audit checks must be objects")
        if raw.get("code_hash", source_hash(code)) != source_hash(code):
            raise ValueError("Sandbox audit returned evidence for a different source hash")
        passed = sum(check.get("status") == "passed" for check in checks)
        failed = sum(check.get("status") == "failed" for check in checks)
        status = raw.get("status", "error")
        if status == "passed" and (
            not raw["executed"] or not checks or failed or passed != len(checks)
        ):
            raise ValueError("Skipped, empty or failed checks cannot authorize a pass")
        if status not in {"passed", "failed", "error", "not_executed"}:
            raise ValueError("Unknown sandbox audit status")
        result = {
            **raw,
            "code_hash": source_hash(code),
            "acceptance_id": task_id,
            "passed_count": passed,
            "failed_count": failed,
        }
    except Exception as error:
        result = {**_unavailable_audit(code, task_id, str(error)), "status": "error"}
    result["elapsed_seconds"] = round(time.perf_counter() - started, 4)
    return result


def _completed_audit(result):
    return bool(result.get("executed")) and result.get("status") in {"passed", "failed"}


def summarize(entries, *, mode):
    """Keep every planned case, including unavailable audits and budget stops."""
    eligible = [entry for entry in entries if entry["expectation"] != "safe_stop"]
    opportunities = [
        entry
        for entry in eligible
        if _completed_audit(entry.get("initial_audit", {}))
        and entry["initial_audit"]["status"] == "failed"
    ]
    paired = [
        entry
        for entry in eligible
        if _completed_audit(entry.get("initial_audit", {}))
        and _completed_audit(entry.get("final_audit", {}))
    ]
    audit_counts = {}
    for stage in ("initial", "final"):
        audits = [entry.get(stage + "_audit", {}) for entry in eligible]
        complete = sum(_completed_audit(audit) for audit in audits)
        passed = sum(_completed_audit(audit) and audit["status"] == "passed" for audit in audits)
        audit_counts[stage] = {
            "eligible_cases": len(eligible),
            "completed": complete,
            "passed": passed,
            "failed": sum(
                _completed_audit(audit) and audit["status"] == "failed" for audit in audits
            ),
            "unavailable": len(eligible) - complete,
            "pass_fraction_among_completed": passed / complete if complete else None,
        }
    workflow_metrics = Counter()
    for entry in entries:
        for key, value in entry.get("metrics", {}).items():
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                workflow_metrics[key] += value
    real = mode == "real_live"
    return {
        "planned_cases": len(entries),
        "attempted_cases": sum(bool(entry.get("run_id")) for entry in entries),
        "not_run_budget": sum(entry.get("case_status") == "not_run_budget" for entry in entries),
        "not_run_after_error": sum(
            entry.get("case_status") == "not_run_error" for entry in entries
        ),
        "workflow_exceptions": sum(
            entry.get("case_status") == "workflow_error" for entry in entries
        ),
        "workflow_statuses": dict(
            Counter(entry.get("workflow_status", "not_run") for entry in entries)
        ),
        "functional_audits": audit_counts,
        "repair_opportunities": len(opportunities),
        "repair_successes": sum(
            entry.get("final_audit", {}).get("status") == "passed"
            and _completed_audit(entry.get("final_audit", {}))
            for entry in opportunities
        ),
        "repair_failures": sum(
            entry.get("final_audit", {}).get("status") == "failed"
            and _completed_audit(entry.get("final_audit", {}))
            for entry in opportunities
        ),
        "repair_outcome_unknown": sum(
            not _completed_audit(entry.get("final_audit", {})) for entry in opportunities
        ),
        "regressions": sum(
            entry["initial_audit"]["status"] == "passed"
            and entry["final_audit"]["status"] == "failed"
            for entry in paired
        ),
        "changed_clean_controls": sum(
            entry["expectation"] == "clean_control" and entry.get("code_changed", False)
            for entry in entries
        ),
        "no_progress_cases": sum(
            entry.get("termination_reason") in {"no_progress", "repeated_code"} for entry in entries
        ),
        "budget_stopped_cases": sum(
            entry.get("termination_reason") in {"budget_exhausted", "fix_budget_exhausted"}
            for entry in entries
        ),
        "safe_stop_cases": [
            {
                "id": entry["id"],
                "status": entry.get("workflow_status", "not_run"),
                "observed_safe_stop": entry.get("workflow_status") == "needs_attention",
            }
            for entry in entries
            if entry["expectation"] == "safe_stop"
        ],
        "three_role_generation_cases": [
            entry["id"]
            for entry in entries
            if entry["input_mode"] == "generation" and entry.get("all_three_roles_executed")
        ],
        "actual_model_calls": workflow_metrics["model_calls"] if real else 0,
        "actual_prompt_tokens": workflow_metrics["prompt_tokens"] if real else 0,
        "actual_completion_tokens": workflow_metrics["completion_tokens"] if real else 0,
        "actual_http_attempts": workflow_metrics["http_attempts"] if real else 0,
        "scripted_model_calls": workflow_metrics["model_calls"] if not real else 0,
        "workflow_metrics": dict(workflow_metrics),
    }


def _save_report(report, output_path, secret=""):
    if output_path is None:
        return
    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(report, ensure_ascii=False, indent=2)
    if secret:
        serialized = serialized.replace(secret, "[credential redacted]")
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(serialized, encoding="utf-8")
    replace_report(temporary, target)


def run_evaluation(
    cases,
    *,
    mode,
    db_path,
    output_path=None,
    settings=None,
    audit_executor=None,
    service_factory=None,
    max_total_model_calls=60,
    max_total_tokens=300_000,
    audit_timeout=10,
    on_case=None,
):
    if mode not in EVALUATION_MODES:
        raise ValueError("Explicitly select offline_smoke or real_live")
    if any(
        isinstance(value, bool) or not isinstance(value, int) or value < 0
        for value in (max_total_model_calls, max_total_tokens)
    ):
        raise ValueError("Evaluation budgets must be nonnegative integers")
    if mode == "real_live" and settings is None and service_factory is None:
        raise ValueError("real_live requires explicit runtime Settings")
    cases = [
        case if isinstance(case, EvaluationCase) else EvaluationCase.model_validate(case)
        for case in cases
    ]
    if len({case.id for case in cases}) != len(cases):
        raise ValueError("Duplicate evaluation case ID")
    secret = getattr(settings, "api_key", "")
    manifest = json.dumps([case.model_dump() for case in cases], sort_keys=True, ensure_ascii=False)
    report = {
        "schema_version": 1,
        "evaluation_mode": mode,
        "real_api_used": False,
        "audit_feedback_to_agents": False,
        "planned_case_ids": [case.id for case in cases],
        "case_manifest_sha256": source_hash(manifest),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "budget_limits": {"model_calls": max_total_model_calls, "tokens": max_total_tokens},
        "entries": [],
        "summary": {},
    }
    used_calls = used_tokens = 0
    halt_after_error = None
    for case in cases:
        spec, uploaded, task = _resolve_case(case)
        entry = {
            "id": case.id,
            "task_id": case.task_id,
            "input_mode": case.input_mode,
            "expectation": case.expectation,
            "case_status": "pending",
            "task": task,
            "audit_contract_sha256": hashlib.sha256(
                build_harness(case.task_id, holdout=True).encode()
            ).hexdigest(),
            "initial_audit": _unavailable_audit(uploaded, case.task_id, "Workflow not run"),
            "final_audit": _unavailable_audit(None, case.task_id, "Workflow not run"),
        }
        report["entries"].append(entry)
        if mode == "real_live" and (
            used_calls >= max_total_model_calls or used_tokens >= max_total_tokens
        ):
            entry.update(case_status="not_run_budget", error="Global evaluation budget exhausted")
            report["summary"] = summarize(report["entries"], mode=mode)
            _save_report(report, output_path, secret)
            continue
        if mode == "real_live" and halt_after_error is not None:
            entry.update(
                case_status="not_run_error",
                error="Previous unexpected workflow error; no further billable runs were started",
            )
            report["summary"] = summarize(report["entries"], mode=mode)
            _save_report(report, output_path, secret)
            continue
        factory = (
            None if mode == "real_live" else lambda role, _, item=case: _SmokeModel(role, item)
        )
        service = None
        accounted = False
        try:
            constructor = service_factory or WorkflowRunService
            service = constructor(db_path, model_factory=factory)
            limits = {
                "max_model_calls": max_total_model_calls - used_calls
                if mode == "real_live"
                else 30,
                "max_tokens": max_total_tokens - used_tokens if mode == "real_live" else 100_000,
            }
            run_id = service.create_run(
                task,
                source=uploaded,
                filename=spec.filename,
                mode="live",
                acceptance_id=case.task_id,
                max_fix_rounds=case.max_fix_rounds,
                validation_profile=case.validation_profile,
                budget_limits=limits,
            )
            entry["run_id"] = run_id
            _save_report(report, output_path, secret)
            snapshot = service.execute(run_id, settings=settings)
            versions = snapshot.get("versions", [])
            initial = versions[0]["code"] if versions else uploaded
            current = next(
                (
                    item["code"]
                    for item in versions
                    if item["id"] == snapshot.get("current_version")
                ),
                None,
            )
            nodes = {
                event.get("node")
                for event in snapshot.get("events", [])
                if not event.get("inherited")
            }
            roles = {role for role in ("coding", "review", "fix") if role in nodes}
            entry.update(
                case_status="completed",
                workflow_status=snapshot.get("status"),
                termination_reason=snapshot.get("termination_reason"),
                snapshot=snapshot,
                initial_code=initial,
                final_code=current,
                metrics=snapshot.get("metrics", {}),
                roles_executed=sorted(roles),
                all_three_roles_executed=roles == {"coding", "review", "fix"},
                code_changed=initial is not None and current is not None and initial != current,
            )
            if mode == "real_live":
                used_calls += entry["metrics"].get("model_calls", 0)
                used_tokens += entry["metrics"].get("prompt_tokens", 0) + entry["metrics"].get(
                    "completion_tokens", 0
                )
                accounted = True
            # Holdout is executed only after execute() returns; results never re-enter the graph.
            entry["initial_audit"] = _audit(initial, case.task_id, audit_executor, audit_timeout)
            entry["final_audit"] = _audit(current, case.task_id, audit_executor, audit_timeout)
        except Exception as error:
            message = str(error).replace(secret, "[credential redacted]") if secret else str(error)
            entry.update(case_status="workflow_error", error=message)
            if mode == "real_live":
                halt_after_error = message
            if service is not None and entry.get("run_id"):
                try:
                    entry["snapshot"] = service.get_snapshot(entry["run_id"])
                    entry["metrics"] = entry["snapshot"].get("metrics", {})
                except Exception:
                    pass
        finally:
            if mode == "real_live" and not accounted:
                used_calls += entry.get("metrics", {}).get("model_calls", 0)
                used_tokens += entry.get("metrics", {}).get("prompt_tokens", 0) + entry.get(
                    "metrics", {}
                ).get("completion_tokens", 0)
            if service is not None and hasattr(service, "close"):
                service.close()
        report["summary"] = summarize(report["entries"], mode=mode)
        report["real_api_used"] = mode == "real_live" and used_calls > 0
        report["budget_overrun_observed"] = (
            used_calls > max_total_model_calls or used_tokens > max_total_tokens
        )
        _save_report(report, output_path, secret)
        if on_case is not None:
            on_case(entry)
    return report


def _load_executor(reference=None):
    if reference:
        module_name, separator, attribute = reference.partition(":")
        if not separator:
            raise ValueError("Audit executor must be a trusted module:function")
        return getattr(importlib.import_module(module_name), attribute)
    from workflow import validation

    return getattr(validation, "audit_execute", None)


def main():
    parser = argparse.ArgumentParser(description="公开合成任务独立评测；默认不调用真实模型")
    parser.add_argument("--mode", choices=EVALUATION_MODES, default="offline_smoke")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--select", help="Comma-separated case IDs")
    parser.add_argument("--db", type=Path, default=ROOT / "reports" / "workflow-evaluation.sqlite3")
    parser.add_argument(
        "--output", type=Path, default=ROOT / "reports" / "workflow-evaluation.json"
    )
    parser.add_argument("--audit-executor", help="Trusted sandbox adapter module:function")
    parser.add_argument("--max-total-model-calls", type=int, default=60)
    parser.add_argument("--max-total-tokens", type=int, default=300_000)
    args = parser.parse_args()
    cases = load_cases(args.cases)
    if args.select:
        chosen = set(args.select.split(","))
        if not chosen.issubset({case.id for case in cases}):
            parser.error("Unknown case ID in --select")
        cases = [case for case in cases if case.id in chosen]
    settings = Settings.from_env() if args.mode == "real_live" else None
    if settings is not None:
        settings.validate()
    report = run_evaluation(
        cases,
        mode=args.mode,
        db_path=args.db,
        output_path=args.output,
        settings=settings,
        audit_executor=_load_executor(args.audit_executor),
        max_total_model_calls=args.max_total_model_calls,
        max_total_tokens=args.max_total_tokens,
        on_case=lambda entry: print(
            f"{entry['id']}: {entry.get('workflow_status', entry['case_status'])}"
        ),
    )
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))
    print(f"报告：{args.output.resolve()}")
    if args.mode == "offline_smoke":
        print("离线固定响应；模型真实调用为 0，不能作为模型质量成绩。")
    incomplete = any(
        entry["case_status"] != "completed" or not _completed_audit(entry.get("final_audit", {}))
        for entry in report["entries"]
    )
    return 2 if incomplete else 0


if __name__ == "__main__":
    raise SystemExit(main())
