"""Inject models instead of using credentials or making a network request."""

import json
from collections import Counter

from review_agent.config import Settings
from review_agent.llm import ModelError
from review_agent.models import Metrics
from workflow.examples import BUGGY_ORDERS, CORRECT_ORDERS, ORDER_TASK
from workflow.service import WorkflowRunService

SETTINGS = Settings(api_key="injected-unit-test-value", model="test-model")


class ScriptedRoleModel:
    def __init__(self, role, calls, *, coding=CORRECT_ORDERS, fail_review=None, stale_fix=False):
        self.role = role
        self.calls = calls
        self.coding = coding
        self.fail_review = fail_review
        self.stale_fix = stale_fix
        self.metrics = Metrics()

    def complete(self, messages, **kwargs):
        self.calls[self.role] += 1
        self.metrics.model_calls += 1
        self.metrics.prompt_tokens += 50
        self.metrics.completion_tokens += 30
        if self.role == "coding":
            content = (
                self.coding
                if self.coding == "invalid JSON"
                else json.dumps({"code": self.coding, "summary": "Synthetic test candidate"})
            )
        elif self.role == "review":
            if self.fail_review is not None and self.fail_review["remaining"]:
                self.fail_review["remaining"] -= 1
                raise ModelError("Synthetic API outage; no real HTTP request was made.")
            if "任务核对阶段" in messages[0]["content"]:
                content = json.dumps({"status": "met", "reason": "Synthetic contract fixture"})
            else:
                content = json.dumps(
                    {"summary": "Synthetic review", "findings": [], "limitations": []}
                )
        else:
            payload = json.loads(messages[1]["content"])
            content = json.dumps(
                {
                    "base_code_hash": "0" * 64 if self.stale_fix else payload["base_code_hash"],
                    "revised_code": CORRECT_ORDERS,
                    "addressed_finding_ids": [],
                    "change_summary": "Synthetic repair",
                    "unresolved_items": [],
                }
            )
        return {"role": "assistant", "content": content}


def test_invalid_coding_output_is_bounded_and_never_becomes_a_code_version(tmp_path):
    calls = Counter()
    service = WorkflowRunService(
        tmp_path / "invalid.sqlite3",
        model_factory=lambda role, _: ScriptedRoleModel(role, calls, coding="invalid JSON"),
    )
    run_id = service.create_run(ORDER_TASK, mode="live")
    result = service.execute(run_id, settings=SETTINGS)

    assert result["status"] in {"failed", "needs_attention", "blocked"}
    assert result["versions"] == []
    assert result["error"]
    assert 1 <= calls["coding"] <= 2
    assert not calls["review"] and not calls["fix"]


def test_api_failure_after_coding_recovers_without_recoding(tmp_path):
    calls = Counter()
    fail_review = {"remaining": 1}

    def factory(role, _):
        return ScriptedRoleModel(role, calls, fail_review=fail_review)

    db_path = tmp_path / "recover.sqlite3"
    service = WorkflowRunService(db_path, model_factory=factory)
    run_id = service.create_run(ORDER_TASK, mode="live")
    blocked = service.execute(run_id, settings=SETTINGS)
    assert blocked["status"] == "blocked"
    assert blocked["recoverable"] is True
    assert len(blocked["versions"]) == 1
    assert calls["coding"] == 1
    original = blocked["versions"][0].copy()

    result = WorkflowRunService(db_path, model_factory=factory).execute(run_id, settings=SETTINGS)
    assert result["status"] == "completed_static"
    assert result["versions"][0] == original
    assert calls["coding"] == 1
    assert calls["review"] > 1


def test_fix_for_an_old_code_hash_is_rejected_and_original_remains_current(tmp_path):
    calls = Counter()
    service = WorkflowRunService(
        tmp_path / "stale.sqlite3",
        model_factory=lambda role, _: ScriptedRoleModel(
            role, calls, coding=BUGGY_ORDERS, stale_fix=True
        ),
    )
    run_id = service.create_run(ORDER_TASK, mode="live", max_fix_rounds=1)
    result = service.execute(run_id, settings=SETTINGS)

    assert calls["fix"] > 0
    assert result["status"] in {"needs_attention", "failed", "blocked"}
    assert len(result["versions"]) == 1
    assert result["current_version"] == result["original_version"]
    assert result["versions"][0]["code"] == BUGGY_ORDERS
    assert result["error"] or result["termination_reason"]


def test_zero_call_budget_stops_before_sending_the_model_request(tmp_path):
    calls = Counter()
    service = WorkflowRunService(
        tmp_path / "budget.sqlite3",
        model_factory=lambda role, _: ScriptedRoleModel(role, calls),
    )
    run_id = service.create_run(ORDER_TASK, mode="live", budget_limits={"max_model_calls": 0})
    result = service.execute(run_id, settings=SETTINGS)

    assert result["status"] not in {"completed_static", "completed_verified"}
    assert not calls
    assert result["versions"] == []
    assert result["termination_reason"] or result["error"]
