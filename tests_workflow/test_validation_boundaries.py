"""Validation-level routing must preserve the distinction between skipped and executed."""

from workflow.examples import CORRECT_ORDERS, ORDER_TASK
from workflow.models import source_hash
from workflow.service import WorkflowRunService
from workflow.validation import validate_code


def test_static_only_does_not_execute_candidate_or_count_skipped_functional_as_pass(tmp_path):
    marker = tmp_path / "candidate_was_executed.txt"
    code = f"from pathlib import Path\nPath({str(marker)!r}).write_text('ran')\n"
    result = validate_code(code, source_hash(code), "static_only", "custom")

    assert not marker.exists()
    assert result["functional_executed"] is False
    functional = [check for check in result["checks"] if check["id"] == "functional"]
    assert functional and all(check["status"] == "skipped" for check in functional)
    assert result["passed_count"] == sum(check["status"] == "passed" for check in result["checks"])


def test_required_functional_validation_without_docker_reports_environment_error(monkeypatch):
    monkeypatch.setattr("workflow.validation.shutil.which", lambda _: None)
    result = validate_code(
        CORRECT_ORDERS, source_hash(CORRECT_ORDERS), "functional_required", "orders-v1"
    )

    assert result["status"] == "error"
    assert result["functional_executed"] is False
    assert any(check["id"] == "docker" and check["status"] == "error" for check in result["checks"])


def test_unknown_contract_is_not_silently_given_the_orders_functional_suite():
    code = "def custom(value):\n    return value\n"
    result = validate_code(code, source_hash(code), "functional_required", "custom")

    assert result["status"] == "error"
    assert result["functional_executed"] is False
    assert any(
        check["id"] == "functional" and check["status"] == "error" for check in result["checks"]
    )


def test_missing_functional_environment_blocks_run_without_downgrading_profile(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("workflow.validation.shutil.which", lambda _: None)
    service = WorkflowRunService(tmp_path / "blocked.sqlite3")
    run_id = service.create_run(
        ORDER_TASK, scenario="clean", validation_profile="functional_required"
    )
    result = service.execute(run_id)

    assert result["status"] == "blocked"
    assert result["recoverable"] is True
    assert result["validation_profile"] == "functional_required"
    assert result["validation"]["functional_executed"] is False
    assert result["validation"]["status"] == "error"
    assert result["status"] not in {"completed_static", "completed_verified"}


def test_validation_for_an_old_hash_cannot_authorize_the_current_code(tmp_path):
    def stale_result(code, code_hash, profile, acceptance_id, **kwargs):
        result = validate_code(code, code_hash, profile, acceptance_id, **kwargs)
        result["code_hash"] = "old-version-hash"
        return result

    service = WorkflowRunService(
        tmp_path / "stale-validation.sqlite3", validation_runner=stale_result
    )
    run_id = service.create_run(ORDER_TASK, scenario="clean")
    result = service.execute(run_id)

    assert result["validation"]["status"] == "passed"
    assert result["status"] == "needs_attention"
    assert result["termination_reason"] == "stale_evidence"


def test_passed_checks_without_actual_functional_execution_are_insufficient(tmp_path):
    def skipped_functional(code, code_hash, profile, acceptance_id, **kwargs):
        result = validate_code(code, code_hash, "static_only", acceptance_id, **kwargs)
        assert result["status"] == "passed"
        return result

    service = WorkflowRunService(
        tmp_path / "missing-functional-evidence.sqlite3", validation_runner=skipped_functional
    )
    run_id = service.create_run(
        ORDER_TASK, scenario="clean", validation_profile="functional_required"
    )
    result = service.execute(run_id)

    assert result["validation"]["status"] == "passed"
    assert result["validation"]["functional_executed"] is False
    assert result["status"] == "needs_attention"
    assert result["termination_reason"] == "functional_not_verified"
