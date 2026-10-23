import copy
import json
from collections import Counter
from dataclasses import replace

import pytest

from review_agent.config import Settings
from review_agent.llm import TransientModelError
from review_agent.models import Metrics
from workflow import agents
from workflow.focused_review import FocusedReview, Probe, execute_probes, lost_passes, probe_id
from workflow.registry import FunctionCase, TaskSpec, register_task_spec
from workflow.service import WorkflowRunService
from workflow.validation import validate_code

TASK = "实现 identity(n)，返回输入整数 n；不得修改输入。"
SOURCE = "def identity(n):\n    return n if n == 0 else n + 1\n"
FIXED = "def identity(n):\n    return n\n"
SETTINGS = Settings(api_key="synthetic-test", model="injected")


@pytest.fixture
def contract(monkeypatch):
    from workflow import registry
    monkeypatch.setattr(registry, "_TASKS", dict(registry._TASKS))
    register_task_spec(TaskSpec("focused-test", "identity", TASK, "identity", ("n",),
        (FunctionCase("zero", (0,), 0), FunctionCase("two", (2,), 2)), (), FIXED))
    return "focused-test"


def probe():
    p = {"title": "负整数", "contract_quote": "返回输入整数 n", "args": [-2], "expected": -2,
         "raises": None, "preserve_input": False}
    return {**p, "id": probe_id(p)}


class Model:
    def __init__(self, role, calls, code):
        self.role, self.calls, self.code = role, calls, code
        self.metrics = Metrics()

    def complete(self, messages, **kwargs):
        self.calls[self.role] += 1
        self.metrics.model_calls += 1
        self.metrics.prompt_tokens += 100
        self.metrics.completion_tokens += 50
        payload = json.loads(messages[1]["content"])
        if self.role == "review":
            result = {"summary": "核对负整数", "requirements_status": "gap", "requirements_reason": "返回值不符",
                      "probes": [{k: v for k, v in probe().items() if k != "id"}]}
        else:
            result = {"base_code_hash": payload["base_code_hash"], "revised_code": self.code,
                      "addressed_finding_ids": [probe()["id"]], "change_summary": "修正返回值"}
        return {"content": json.dumps(result), "finish_reason": "stop"}

    def close(self):
        pass


@pytest.mark.parametrize("parallel", [False, True])
def test_review_once_replay_and_reuse_candidate_validation(tmp_path, contract, parallel):
    calls = Counter()
    def validate(*args, **kw):
        calls["validate"] += 1
        return validate_code(*args, **kw)
    with_service = WorkflowRunService(tmp_path / "flow.db", model_factory=lambda r, _: Model(r, calls, FIXED), validation_runner=validate)
    try:
        run = with_service.create_run(TASK, source=SOURCE, mode="live", acceptance_id=contract,
            validation_profile="functional_portable", workflow_config={"parallel_checks": parallel})
        data = with_service.execute(run, settings=SETTINGS)
        assert data["status"] == "completed_verified"
        assert calls == {"review": 1, "fix": 1, "validate": 2}
        assert data["review"]["replayed"] is True
        assert data["repair_records"][0]["adopted"] is True
        assert len(data["versions"]) == 2
    finally:
        with_service.close()


@pytest.mark.parametrize("canvas", [False, True])
def test_candidate_regression_keeps_current_source_and_rejection_evidence(tmp_path, contract, canvas):
    from workflow.canvas import template_graph
    calls = Counter()
    service = WorkflowRunService(tmp_path / "reject.db", model_factory=lambda r, _: Model(r, calls, "def identity(n):\n    return 1\n"))
    plan = {"canvas": template_graph()} if canvas else {}
    try:
        run = service.create_run(TASK, source=SOURCE, mode="live", acceptance_id=contract,
            validation_profile="functional_portable", workflow_config=plan)
        data = service.execute(run, settings=SETTINGS)
        assert data["termination_reason"] == "candidate_rejected"
        assert data["current_version"] == data["original_version"]
        assert len(data["versions"]) == 1
        record = data["repair_records"][0]
        assert record["adopted"] is False and "zero" in record["regressed_checks"]
        assert record["candidate_validation"]["code_hash"] == record["candidate_hash"]
        assert "return 1" in record["revised_code"]
    finally:
        service.close()


def test_matching_probe_refutes_a_claim_instead_of_forcing_fix(tmp_path, contract):
    calls = Counter()
    service = WorkflowRunService(tmp_path / "false.db", model_factory=lambda r, _: Model(r, calls, FIXED))
    try:
        run = service.create_run(TASK, source=FIXED, mode="live", acceptance_id=contract, validation_profile="functional_portable")
        data = service.execute(run, settings=SETTINGS)
        assert calls == {"review": 1}
        assert data["review"]["findings"] == []
        assert data["review"]["requirements_status"] == "unknown"
        assert data["status"] == "needs_attention"
    finally:
        service.close()


def test_untrusted_probe_cannot_force_edit_or_veto_correct_candidate(tmp_path, contract, monkeypatch):
    from workflow.models import FixOutput
    calls = Counter()
    service = WorkflowRunService(tmp_path / "oracle.db", model_factory=lambda r, _: Model(r, calls, FIXED))
    try:
        run = service.create_run(TASK, source=SOURCE, mode="live", acceptance_id=contract, validation_profile="functional_portable")
        data = service.execute(run, settings=SETTINGS, stop_after="validate")
        wrong = {**probe(), "args": [0], "expected": 1}
        wrong["id"] = probe_id(wrong)
        # A model can mistakenly describe the old, buggy behaviour as desired.
        wrong.update(args=[2], expected=3)
        wrong["id"] = probe_id(wrong)
        data["review"]["probes"] = [wrong]
        data["review"]["probe_execution"] = execute_probes(SOURCE, "identity", [wrong])
        data["review"]["findings"] = [{"id": wrong["id"], "title": "untrusted", "severity": "high", "oracle": "model_authored", "verification": "reviewed"}]
        data["review"]["requirements_status"] = "unknown"
        payload = agents.repair_context(data, SOURCE)
        assert all(t["id"] != wrong["id"] for t in payload["repair_targets"])
        monkeypatch.setattr(agents, "fix", lambda *a: FixOutput(base_code_hash=data["review"]["code_hash"], revised_code=FIXED))
        service._perform("fix", data, SETTINGS, __import__("time").monotonic())
        record = data["repair_records"][-1]
        assert record["adopted"] is True and record["regressed_checks"] == []
        assert record["advisory_probe_regressions"] == [wrong["id"]]
        data["review"] = {"code_hash": data["validation"]["code_hash"], "complete": True,
            "requirements_status": "unknown", "findings": [{"title": "untrusted", "severity": "high", "oracle": "model_authored"}]}
        service._gate(data)
        assert data["status"] == "needs_attention" and data["termination_reason"] == "unresolved_review"
        assert data["next_node"] != "fix"
    finally:
        service.close()


def test_probe_protocol_failure_does_not_become_an_observed_defect(monkeypatch):
    monkeypatch.setattr("workflow.focused_review.execute_function", lambda *a, **k: {"executed": True, "results": []})
    result = execute_probes(SOURCE, "identity", [probe()])
    assert result["error"] and result["checks"] == []


def test_missing_skipped_and_error_checks_are_not_preserved():
    before = {"checks": [{"id": k, "status": "passed"} for k in ["a", "b", "c", "d"]]}
    after = {"checks": [{"id": "a", "status": "passed"}, {"id": "b", "status": "skipped"}, {"id": "d", "status": "error"}]}
    assert lost_passes(before, after) == ["b", "c", "d"]


def test_short_chinese_contract_quote_is_valid():
    item = Probe(title="空区间", contract_quote="空区间", args=[[[0, 0]]], expected=[])
    assert item.contract_quote == "空区间"


def test_format_retry_supplies_specific_schema_feedback_without_echoing_values():
    calls = []
    class InvalidThenValid:
        def complete(self, messages, **kwargs):
            calls.append(copy.deepcopy(messages))
            return {"content": json.dumps({"summary": "ok", "requirements_status": "not-a-status" if len(calls) == 1 else "met",
                "requirements_reason": "fixture", "probes": []})}
    result = agents.structured(InvalidThenValid(), FocusedReview, "review", {})
    assert result.requirements_status == "met"
    assert "literal_error" in calls[1][-1]["content"]
    assert "requirements_status" in calls[1][-1]["content"]
    assert "not-a-status" not in calls[1][-1]["content"]


@pytest.mark.parametrize("limit", [1, 2])
def test_transport_retry_is_charged_and_cannot_bypass_call_limit(limit):
    import time
    class Transport:
        settings = replace(SETTINGS, max_retries=0)
        metrics = Metrics()
        def complete(self, *a, **kw):
            self.metrics.model_calls += 1
            if self.metrics.model_calls == 1:
                raise TransientModelError("fixture")
            return {"content": "{}"}
    data = {"mode": "live", "acceptance_id": "test", "validation_profile": "functional_portable",
            "workflow_config": {"review_strategy": "focused"}, "metrics": {"model_calls": 0,
            "http_attempts": 0, "prompt_tokens": 0, "completion_tokens": 0, "active_seconds": 0},
            "budget_limits": {"max_model_calls": limit, "max_tokens": 10000, "max_active_seconds": 60}}
    model = agents.BudgetedModel(Transport(), data, time.monotonic())
    if limit == 1:
        with pytest.raises(agents.BudgetExceeded):
            model.complete([])
    else:
        assert model.complete([]) == {"content": "{}"}
    assert data["metrics"]["model_calls"] == limit


def test_review_plan_replay_preserves_unknown_measurement(monkeypatch, tmp_path, contract):
    # Unknown goals cannot be converted to 'met' through a cached test plan.
    calls = Counter()
    service = WorkflowRunService(tmp_path / "unknown.db", model_factory=lambda r, _: Model(r, calls, FIXED))
    try:
        run = service.create_run(TASK, source=SOURCE, mode="live", acceptance_id=contract, validation_profile="functional_portable")
        data = service.execute(run, settings=SETTINGS, stop_after="review")
        state = copy.deepcopy(data)
        state["task"] = "提高识别准确率"
        result = agents.review(state, SOURCE, SETTINGS, service.model_factory, __import__("time").monotonic())
        assert result["requirements_status"] == "unknown"
    finally:
        service.close()
