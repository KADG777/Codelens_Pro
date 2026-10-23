"""Evaluation accounting and holdout isolation, without network or candidate execution."""

import copy
import json

import pytest

from review_agent.config import Settings
from scripts.evaluation.evaluate_workflow import _audit, load_cases, run_evaluation, summarize
from workflow.models import source_hash
from workflow.registry import get_task_spec


def audit_outcome(status, *, executed=True):
    return {
        "status": status,
        "executed": executed,
        "checks": [{"id": "audit-fixture", "status": status, "detail": "Synthetic audit"}]
        if executed
        else [],
        "passed_count": int(status == "passed"),
        "failed_count": int(status == "failed"),
    }


class FakeFactory:
    """Only evaluator/service plumbing is faked; no generated code is imported."""

    def __init__(self, *, calls_per_run=3, tokens_per_run=100, raises=False):
        self.creates = []
        self.executions = []
        self.factories = []
        self.calls_per_run = calls_per_run
        self.tokens_per_run = tokens_per_run
        self.raises = raises

    def __call__(self, db_path, model_factory=None):
        owner = self
        owner.factories.append(model_factory)

        class FakeService:
            def create_run(self, task, **kwargs):
                owner.creates.append({"task": task, **kwargs})
                self.kwargs = kwargs
                self.run_id = f"run-{len(owner.creates)}"
                return self.run_id

            def execute(self, run_id, **kwargs):
                owner.executions.append(run_id)
                spec = get_task_spec(self.kwargs["acceptance_id"])
                initial = (
                    self.kwargs["source"] or spec.buggy_code or "def fixture():\n    return None\n"
                )
                final = spec.reference_code
                versions = [
                    {"id": "v0", "code": initial, "hash": source_hash(initial)},
                    {"id": "v1", "code": final, "hash": source_hash(final)},
                ]
                self.snapshot = {
                    "run_id": run_id,
                    "status": "completed_verified",
                    "termination_reason": "fixture",
                    "versions": versions,
                    "current_version": "v1",
                    "original_version": "v0",
                    "metrics": {
                        "model_calls": owner.calls_per_run,
                        "prompt_tokens": owner.tokens_per_run,
                        "completion_tokens": 0,
                        "active_seconds": 0.1,
                    },
                    "events": [{"node": node} for node in ("coding", "review", "fix")],
                }
                if owner.raises:
                    raise RuntimeError("synthetic secret-value failure after committed calls")
                return copy.deepcopy(self.snapshot)

            def get_snapshot(self, run_id):
                return copy.deepcopy(self.snapshot)

            def close(self):
                pass

        return FakeService()


def test_case_manifest_covers_ten_contracts_and_explicit_controls():
    cases = load_cases()
    assert len(cases) == 12
    assert len({case.task_id for case in cases}) == 10
    assert sum(case.input_mode == "generation" for case in cases) == 2
    assert sum(case.expectation == "clean_control" for case in cases) == 1
    assert sum(case.expectation == "safe_stop" for case in cases) == 1
    assert all(
        case.validation_profile == "functional_portable"
        for case in cases
        if case.expectation != "safe_stop"
    )


def test_summary_retains_unknown_audits_and_does_not_turn_them_into_failed_repairs():
    def row(name, initial=None, final=None, expectation="repair", **extra):
        return {
            "id": name,
            "input_mode": "upload",
            "expectation": expectation,
            "initial_audit": initial or {},
            "final_audit": final or {},
            **extra,
        }

    entries = [
        row("repair", audit_outcome("failed"), audit_outcome("passed")),
        row("regression", audit_outcome("passed"), audit_outcome("failed")),
        row("unknown", audit_outcome("failed"), audit_outcome("not_executed", executed=False)),
        row(
            "clean",
            audit_outcome("passed"),
            audit_outcome("passed"),
            "clean_control",
            code_changed=True,
        ),
        row("budget", case_status="not_run_budget"),
        row("safe", expectation="safe_stop", workflow_status="needs_attention"),
    ]
    summary = summarize(entries, mode="real_live")

    assert summary["planned_cases"] == 6
    assert summary["functional_audits"]["initial"] == {
        "eligible_cases": 5,
        "completed": 4,
        "passed": 2,
        "failed": 2,
        "unavailable": 1,
        "pass_fraction_among_completed": 0.5,
    }
    assert summary["functional_audits"]["final"]["unavailable"] == 2
    assert summary["repair_opportunities"] == 2
    assert summary["repair_successes"] == 1
    assert summary["repair_outcome_unknown"] == 1
    assert summary["regressions"] == 1
    assert summary["changed_clean_controls"] == 1
    assert summary["not_run_budget"] == 1
    assert summary["safe_stop_cases"][0]["observed_safe_stop"] is True


def test_holdout_runs_after_workflow_and_never_enters_create_run_or_agent_input(tmp_path):
    case = load_cases()[0]
    factory = FakeFactory()
    audits = []

    def executor(code, acceptance_id, **kwargs):
        assert factory.executions == ["run-1"]
        assert kwargs["holdout"] is True
        audits.append((source_hash(code), acceptance_id))
        correct = code == get_task_spec(acceptance_id).reference_code
        return audit_outcome("passed" if correct else "failed")

    report = run_evaluation(
        [case],
        mode="real_live",
        db_path=tmp_path / "unused.db",
        service_factory=factory,
        audit_executor=executor,
    )

    assert len(audits) == 2
    assert factory.factories == [None]
    submitted = factory.creates[0]
    assert submitted["source"] is None
    assert submitted["acceptance_id"] == case.task_id
    assert submitted["task"] == get_task_spec(case.task_id).task
    assert not any(
        key in submitted for key in ("holdout", "reference_code", "correct_code", "checks")
    )
    assert report["audit_feedback_to_agents"] is False
    assert report["summary"]["repair_successes"] == 1
    assert report["entries"][0]["initial_code"]
    assert report["entries"][0]["final_code"]


def test_review_success_without_executor_is_not_functional_success(tmp_path):
    factory = FakeFactory()
    report = run_evaluation(
        [load_cases()[0]], mode="real_live", db_path=tmp_path / "unused.db", service_factory=factory
    )

    assert report["entries"][0]["workflow_status"] == "completed_verified"
    assert report["entries"][0]["final_audit"]["status"] == "not_executed"
    assert report["summary"]["functional_audits"]["final"]["passed"] == 0
    assert report["summary"]["functional_audits"]["final"]["unavailable"] == 1


def test_changed_code_and_successful_review_do_not_hide_a_failed_final_audit():
    summary = summarize(
        [
            {
                "id": "still_wrong",
                "input_mode": "upload",
                "expectation": "repair",
                "workflow_status": "completed_verified",
                "code_changed": True,
                "initial_audit": audit_outcome("failed"),
                "final_audit": audit_outcome("failed"),
            }
        ],
        mode="real_live",
    )
    assert summary["repair_opportunities"] == 1
    assert summary["repair_failures"] == 1
    assert summary["repair_successes"] == 0
    assert summary["regressions"] == 0


def test_global_budget_preserves_unrun_cases_and_prevents_another_model_run(tmp_path):
    factory = FakeFactory(calls_per_run=3)
    report = run_evaluation(
        load_cases()[:2],
        mode="real_live",
        db_path=tmp_path / "unused.db",
        service_factory=factory,
        max_total_model_calls=3,
    )

    assert len(factory.creates) == 1
    assert factory.creates[0]["budget_limits"]["max_model_calls"] == 3
    assert len(report["entries"]) == 2
    assert report["entries"][1]["case_status"] == "not_run_budget"
    assert report["summary"]["actual_model_calls"] == 3
    assert report["summary"]["functional_audits"]["final"]["eligible_cases"] == 2
    assert report["summary"]["functional_audits"]["final"]["unavailable"] == 2


def test_committed_usage_after_workflow_exception_still_consumes_global_budget(tmp_path):
    factory = FakeFactory(calls_per_run=3, raises=True)
    output = tmp_path / "report.json"
    report = run_evaluation(
        load_cases()[:2],
        mode="real_live",
        db_path=tmp_path / "unused.db",
        output_path=output,
        settings=Settings(api_key="secret-value"),
        service_factory=factory,
        max_total_model_calls=3,
    )

    assert len(factory.creates) == 1
    assert report["entries"][0]["case_status"] == "workflow_error"
    assert report["entries"][0]["snapshot"]["versions"]
    assert report["entries"][1]["case_status"] == "not_run_budget"
    assert report["summary"]["actual_model_calls"] == 3
    persisted = json.loads(output.read_text(encoding="utf-8"))
    assert persisted["entries"][0]["error"].endswith("failure after committed calls")
    assert "secret-value" not in output.read_text(encoding="utf-8")


def test_offline_smoke_injects_scripted_models_and_reports_zero_actual_api_usage(tmp_path):
    factory = FakeFactory()
    report = run_evaluation(
        [load_cases()[0]],
        mode="offline_smoke",
        db_path=tmp_path / "unused.db",
        service_factory=factory,
    )

    assert callable(factory.factories[0])
    assert report["real_api_used"] is False
    assert report["summary"]["actual_model_calls"] == 0
    assert report["summary"]["actual_prompt_tokens"] == 0
    assert report["summary"]["scripted_model_calls"] == 3


def test_unexpected_workflow_exception_stops_further_billable_cases_even_with_budget(tmp_path):
    factory = FakeFactory(calls_per_run=1, raises=True)
    report = run_evaluation(
        load_cases()[:2],
        mode="real_live",
        db_path=tmp_path / "unused.db",
        service_factory=factory,
        max_total_model_calls=10,
    )
    assert len(factory.creates) == 1
    assert len(report["entries"]) == 2
    assert report["entries"][1]["case_status"] == "not_run_error"
    assert report["summary"]["not_run_after_error"] == 1


@pytest.mark.parametrize(
    "reply",
    [
        {"status": "passed", "executed": False, "checks": []},
        {"status": "passed", "executed": True, "checks": []},
        {**audit_outcome("passed"), "code_hash": "old-source"},
    ],
)
def test_empty_skipped_or_stale_audit_cannot_be_counted_as_pass(reply):
    result = _audit(
        "def f():\n    return 1\n", "text-stats-v1", lambda *_args, **_kwargs: reply, 10
    )
    assert result["status"] == "error"
    assert result["error"]


def test_zero_global_budget_never_constructs_a_model_service(tmp_path):
    def forbidden(*_args, **_kwargs):
        raise AssertionError("No service may be created after the global budget is exhausted")

    report = run_evaluation(
        load_cases()[:2],
        mode="real_live",
        db_path=tmp_path / "unused.db",
        service_factory=forbidden,
        max_total_model_calls=0,
    )

    assert report["real_api_used"] is False
    assert report["summary"]["not_run_budget"] == 2
    assert report["summary"]["actual_model_calls"] == 0
