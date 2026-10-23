import json
from copy import deepcopy

import pytest

from review_agent.config import Settings
from review_agent.models import Metrics
from scripts.experiments.compare_workflows import (
    ExperimentBudget,
    audit_status,
    direct_messages,
    run_direct,
    summarize_pairs,
)
from workflow.agents import BudgetExceeded
from workflow.models import source_hash
from workflow.registry import get_task_spec


def evidence(code="x = 1\n", status="passed"):
    return {"code": code, "audits": {part: {"code_hash": source_hash(code), "executed": True,
        "checks": [{"id": "case", "status": status}]} for part in ("public", "holdout")}}


def test_paired_denominator_includes_unavailable_and_recomputes_hash_evidence():
    rows = [{"split": "fresh", "direct": evidence(), "workflow": evidence(status="failed"), "initial": evidence()},
            {"split": "development", "direct": {}, "workflow": evidence(), "initial": evidence(status="failed")}]
    result = summarize_pairs(rows)
    assert result["all"]["direct"]["pass_rate"] == .5
    assert result["all"]["direct"]["unavailable"] == 1
    assert result["fresh"]["workflow"]["regressions"] == 1
    assert result["fresh"]["workflow"]["regressed_checks"] == 2
    forged = deepcopy(rows[0]["direct"])
    forged["audits"]["holdout"]["code_hash"] = "stale"
    assert audit_status(forged) == "unavailable"
    forged = deepcopy(rows[0]["direct"])
    forged["audits"]["holdout"]["executed"] = False
    assert audit_status(forged) == "unavailable"


def test_direct_arm_receives_contract_and_same_source_but_no_test_answers():
    spec = get_task_spec("clamp-v1")
    case = {"task_id": spec.id, "task": spec.task, "source": "def clamp(value, lower, upper):\n    return value\n"}
    messages = direct_messages(case)
    payload = json.loads(messages[1]["content"])
    assert payload["code"] == case["source"] and payload["task"] == spec.task
    assert payload["acceptance_contract"] == spec.model_contract()
    assert all(c.id not in str(messages) for c in (*spec.public_cases, *spec.holdout_cases))


def test_direct_invalid_output_has_one_call_and_no_format_retry():
    calls = []
    class Invalid:
        metrics = Metrics()
        def complete(self, *args, **kwargs):
            calls.append(1)
            self.metrics.model_calls += 1
            return {"content": "not json"}
        def close(self):
            pass
    spec = get_task_spec("clamp-v1")
    result = run_direct({"task_id": spec.id, "task": spec.task}, Settings(), lambda *args: Invalid())
    assert len(calls) == 1 and result["code"] is None and result["status"] == "failed"


def test_global_budget_blocks_next_arm_before_request():
    class Client:
        def __init__(self, settings):
            self.metrics = Metrics()
        def complete(self, *args, **kwargs):
            self.metrics.prompt_tokens += 100
            return {}
        def close(self):
            pass
    budget = ExperimentBudget(1, 5000, Client)
    first = budget.factory("direct", Settings())
    first.complete([], max_tokens=1000)
    with pytest.raises(BudgetExceeded):
        budget.factory("review", Settings()).complete([], max_tokens=1000)
    assert budget.calls == 1 and budget.actual_tokens == 100


def test_report_publication_retries_only_transient_file_locks(tmp_path, monkeypatch):
    from pathlib import Path

    from workflow.storage import replace_report
    source, target = tmp_path / "pending", tmp_path / "report"
    source.write_text("new", encoding="utf-8")
    target.write_text("old", encoding="utf-8")
    calls = []
    original = Path.replace
    def locked(self, destination):
        calls.append(1)
        if len(calls) < 3:
            raise PermissionError("temporary reader lock")
        return original(self, destination)
    monkeypatch.setattr(Path, "replace", locked)
    monkeypatch.setattr("workflow.storage.time.sleep", lambda delay: None)
    replace_report(source, target)
    assert target.read_text() == "new" and len(calls) == 3
    monkeypatch.setattr(Path, "replace", lambda *args: (_ for _ in ()).throw(PermissionError("persistent")))
    with pytest.raises(PermissionError):
        replace_report(source, target)
