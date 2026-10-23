import hashlib
import json
from copy import deepcopy
from pathlib import Path
from threading import Event, Thread

import pytest

from compare_agent_value import ARMS, run_feedback, summarize
from compare_workflows import ExperimentBudget, _audits, audit_status, load_suite
from review_agent.config import Settings
from review_agent.models import Metrics
from workflow.agents import BudgetExceeded
from workflow.models import source_hash

ROOT = Path(__file__).resolve().parents[1]


def test_published_experiment_fingerprints_and_derived_totals():
    from ui.agent_value_view import load_report
    raw, report, totals = load_report(ROOT / "evals/results/agent-value-live.json")
    assert totals == report["summary"]
    assert hashlib.sha256((ROOT / "evals/agent_value_cases.json").read_bytes()).hexdigest() == report["suite_sha256"]
    recovery = json.loads((ROOT / "evals/results/real-recovery.json").read_text(encoding="utf-8"))
    assert hashlib.sha256(raw).hexdigest() == recovery["primary_sha256"]
    assert recovery["combined_usage"]["calls"] <= 180
    assert recovery["combined_usage"]["charged_tokens"] <= 500_000


REFERENCES = {
"av-csv-v1": '''def parse_csv(text):
    if not text:
        return []
    rows, row, field = [], [], []
    mode = "start"
    i = 0
    while i < len(text):
        ch = text[i]
        if mode == "quoted":
            if ch == '"':
                if i + 1 < len(text) and text[i+1] == '"':
                    field.append('"')
                    i += 1
                else:
                    mode = "closed"
            else:
                field.append(ch)
        elif ch in ",\\n":
            row.append("".join(field))
            field, mode = [], "start"
            if ch == "\\n":
                rows.append(row)
                row = []
        elif mode == "closed":
            raise ValueError("text after quote")
        elif ch == '"':
            if mode != "start":
                raise ValueError("quote in unquoted field")
            mode = "quoted"
        else:
            field.append(ch)
            mode = "plain"
        i += 1
    if mode == "quoted":
        raise ValueError("unclosed quote")
    if text[-1] != "\\n" or mode == "closed":
        row.append("".join(field))
        rows.append(row)
    return rows
''',
"av-pointer-v1": '''import re
def resolve_pointer(document, pointer):
    if not pointer:
        return document
    if not pointer.startswith("/"):
        raise ValueError("prefix")
    value = document
    for raw in pointer[1:].split("/"):
        if re.search(r"~(?![01])", raw):
            raise ValueError("escape")
        token = re.sub(r"~[01]", lambda m: "~" if m[0] == "~0" else "/", raw)
        try:
            if isinstance(value, dict):
                value = value[token]
            elif isinstance(value, list) and re.fullmatch(r"0|[1-9][0-9]*", token):
                value = value[int(token)]
            else:
                raise ValueError("container or index")
        except (KeyError, IndexError):
            raise ValueError("missing") from None
    return value
''',
"av-layers-v1": '''def dependency_layers(dependencies):
    pending = {k: set(v) for k,v in dependencies.items()}
    for values in dependencies.values():
        for value in values:
            pending.setdefault(value, set())
    layers = []
    while pending:
        layer = sorted(k for k,v in pending.items() if not v)
        if not layer:
            raise ValueError("cycle")
        layers.append(layer)
        pending = {k: v - set(layer) for k,v in pending.items() if k not in layer}
    return layers
''',
"av-transfer-v1": '''def apply_transfers(accounts, transfers):
    if not isinstance(accounts, dict) or any(not isinstance(k,str) or type(v) is not int or v < 0 for k,v in accounts.items()):
        raise ValueError("account")
    result = accounts.copy()
    if not isinstance(transfers, list):
        raise ValueError("transfers")
    for item in transfers:
        if not isinstance(item,(list,tuple)) or len(item) != 3:
            raise ValueError("structure")
        sender, receiver, amount = item
        if not isinstance(sender,str) or not isinstance(receiver,str) or sender not in result or receiver not in result:
            raise ValueError("account")
        if type(amount) is not int or amount <= 0 or result[sender] < amount:
            raise ValueError("amount")
        result[sender] -= amount
        result[receiver] += amount
    return result
'''}


@pytest.fixture(scope="module")
def suite():
    import workflow.registry as registry
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(registry, "_TASKS", dict(registry._TASKS))
        yield load_suite(ROOT / "evals/agent_value_cases.json")[0]


@pytest.mark.parametrize("task_id", list(REFERENCES))
def test_frozen_suite_has_satisfiable_public_and_hidden_contracts(suite, task_id):
    code = REFERENCES[task_id]
    assert audit_status({"code": code, "audits": _audits(code, task_id)}) == "passed"


def test_single_feedback_receives_only_public_feedback_and_retains_one_agent(suite):
    inputs = []
    code = REFERENCES["av-transfer-v1"]
    class Model:
        metrics = Metrics()
        def complete(self, messages, **kwargs):
            inputs.append(deepcopy(messages))
            self.metrics.model_calls += 1
            return {"content": json.dumps({"code": code, "summary": "candidate"})}
        def close(self):
            pass
    calls = []
    def validator(source, digest, profile, task_id, **kwargs):
        calls.append(digest)
        return {"status": "failed" if len(calls) == 1 else "passed", "functional_executed": True,
                "checks": [{"id": "public_feedback", "status": "failed" if len(calls) == 1 else "passed"}]}
    created = []
    def factory(*args):
        created.append(1)
        return Model()
    result = run_feedback(suite["cases"][-1], Settings(), factory, validator)
    assert len(inputs) == 2 and len(created) == 1
    assert "public_feedback" in str(inputs[-1])
    assert all(c["id"] not in json.dumps(inputs, ensure_ascii=False) for s in suite["fresh_specs"] for c in s["holdout_cases"])
    assert result["metrics"]["model_calls"] == 2


def test_repeated_summary_counts_unavailable_and_keeps_task_clusters():
    def value(status):
        code = "x = 1\n"
        return {"code": code, "audits": {p: {"code_hash": source_hash(code), "executed": True,
            "checks": [{"id": "c", "status": status}]} for p in ("public", "holdout")}}
    entries = [{"case_id": "same_task", "initial": value("passed"), "direct": value("passed"), "single_feedback": {}, "workflow": value("failed")},
               {"case_id": "same_task", "initial": value("failed"), **{a: value("passed") for a in ARMS}}]
    totals = summarize(entries)
    assert totals["arms"]["single_feedback"]["planned"] == 2
    assert totals["arms"]["single_feedback"]["unavailable"] == 1
    assert totals["arms"]["workflow"]["regressed_checks"] == 2
    assert len(totals["tasks"]) == 1 and totals["paired"]["direct"]["baseline_only_pass"] == 1


def test_concurrent_requests_reserve_global_budget_before_network():
    entered, release = Event(), Event()
    class Client:
        def __init__(self, settings):
            self.metrics = Metrics()
        def complete(self, *args, **kwargs):
            entered.set()
            assert release.wait(5)
            self.metrics.prompt_tokens += 100
            return {}
        def close(self):
            pass
    budget = ExperimentBudget(5, 3000, Client)
    thread = Thread(target=lambda: budget.factory("a", Settings()).complete([], max_tokens=1800))
    thread.start()
    assert entered.wait(5)
    try:
        with pytest.raises(BudgetExceeded):
            budget.factory("b", Settings()).complete([], max_tokens=1800)
    finally:
        release.set()
        thread.join()
    assert budget.calls == 1 and budget.reserved_tokens == 0 and budget.actual_tokens == 100
