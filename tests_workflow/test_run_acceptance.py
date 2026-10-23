"""Public-service acceptance checks with real local tools and a temporary database."""

import hashlib

import pytest

from workflow.examples import CORRECT_ORDERS, ORDER_TASK
from workflow.service import WorkflowRunService

TASK = ORDER_TASK


def digest(code):
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def current(snapshot):
    return next(v for v in snapshot["versions"] if v["id"] == snapshot["current_version"])


@pytest.fixture
def db_path(tmp_path):
    return tmp_path / "workflow.sqlite3"


def test_upload_preserves_original_and_persists_hash_bound_version(db_path):
    service = WorkflowRunService(db_path)
    source = "def double(value):\n    return value * 2\n"
    run_id = service.create_run(TASK, source=source, filename="double.py")
    snapshot = service.get_snapshot(run_id)

    assert snapshot["input_mode"] == "upload"
    assert len(snapshot["versions"]) == 1
    assert current(snapshot)["code"] == source
    assert current(snapshot)["hash"] == digest(source)
    assert snapshot["current_version"] == snapshot["original_version"]
    assert current(snapshot)["parent"] is None
    assert WorkflowRunService(db_path).get_snapshot(run_id)["versions"] == snapshot["versions"]


def test_first_clean_candidate_finishes_without_artificial_fix(db_path):
    service = WorkflowRunService(db_path)
    run_id = service.create_run(TASK, scenario="clean")
    result = service.execute(run_id)

    assert result["status"] == "completed_static"
    assert len(result["versions"]) == 1
    assert not any(event["node"] == "fix" for event in result["events"])
    assert result["validation"]["functional_executed"] is False
    assert result["review"]["code_hash"] == current(result)["hash"]
    assert result["validation"]["code_hash"] == current(result)["hash"]


def test_orders_flow_repairs_source_and_rechecks_the_new_version(db_path):
    service = WorkflowRunService(db_path)
    run_id = service.create_run(TASK, scenario="orders", max_fix_rounds=2)
    result = service.execute(run_id)

    assert result["status"] == "completed_static"
    assert len(result["versions"]) >= 2
    original = next(v for v in result["versions"] if v["id"] == result["original_version"])
    assert original["code"] != current(result)["code"]
    assert original["hash"] != current(result)["hash"]
    assert current(result)["parent"] in {v["id"] for v in result["versions"][:-1]}
    for version in result["versions"]:
        assert version["hash"] == digest(version["code"])
    assert result["review"]["code_hash"] == current(result)["hash"]
    assert result["validation"]["code_hash"] == current(result)["hash"]
    assert result["review"]["complete"] is True
    assert result["validation"]["functional_executed"] is False
    assert {"coding", "review", "validate", "fix"}.issubset(
        {event["node"] for event in result["events"]}
    )


@pytest.mark.parametrize("scenario,max_rounds", [("orders", 0), ("unresolved", 2)])
def test_exhaustion_or_no_progress_keeps_problem_visible(db_path, scenario, max_rounds):
    service = WorkflowRunService(db_path)
    run_id = service.create_run(TASK, scenario=scenario, max_fix_rounds=max_rounds)
    result = service.execute(run_id)

    assert result["status"] == "needs_attention"
    assert result["status"] not in {"completed_static", "completed_verified"}
    assert len(result["versions"]) <= max_rounds + 1
    assert result["termination_reason"]
    assert current(result)["code"]


def test_resume_after_review_retains_committed_coding_and_original_version(db_path):
    service = WorkflowRunService(db_path)
    run_id = service.create_run(TASK, scenario="orders")
    paused = service.execute(run_id, stop_after="review")
    assert paused["status"] == "paused"
    saved_original = dict(paused["versions"][0])
    coding_events = [event for event in paused["events"] if event["node"] == "coding"]
    assert coding_events

    reopened = WorkflowRunService(db_path)
    resumed = reopened.execute(run_id)
    assert resumed["status"] == "completed_static"
    assert resumed["versions"][0] == saved_original
    assert [event for event in resumed["events"] if event["node"] == "coding"] == coding_events
    assert resumed["original_version"] == paused["original_version"]
    assert len({event["id"] for event in resumed["events"]}) == len(resumed["events"])


def test_reading_and_reexecuting_a_completed_run_does_not_create_new_work(db_path):
    service = WorkflowRunService(db_path)
    run_id = service.create_run(TASK, scenario="clean")
    completed = service.execute(run_id)
    for _ in range(2):
        read_back = service.get_snapshot(run_id)
        assert read_back["versions"] == completed["versions"]
        assert read_back["events"] == completed["events"]
    repeated = WorkflowRunService(db_path).execute(run_id)
    assert repeated["versions"] == completed["versions"]
    assert repeated["events"] == completed["events"]


def test_upload_skips_coding_and_static_checks_never_claim_functional_execution(db_path):
    service = WorkflowRunService(db_path)
    source = CORRECT_ORDERS
    run_id = service.create_run(TASK, source=source, validation_profile="static_only")
    result = service.execute(run_id)

    assert not any(event["node"] == "coding" for event in result["events"])
    assert result["validation"]["functional_executed"] is False
    assert result["status"] == "completed_static"
    assert current(result)["code"] == source


@pytest.mark.parametrize(
    "filename,source",
    [
        ("solution.cpp", "int main() { return 0; }\n"),
        ("solution.py", "```python\nvalue = 1\n```"),
        ("solution.py", "def example():\n    ...\n"),
    ],
)
def test_unsupported_language_or_incomplete_source_is_rejected_before_execution(
    db_path, filename, source
):
    service = WorkflowRunService(db_path)
    with pytest.raises(ValueError):
        service.create_run(TASK, source=source, filename=filename)
