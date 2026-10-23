"""Real CPython/Wasm execution, including containment and independent verdicts."""

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from workflow.registry import get_task_spec, list_task_specs
from workflow.validation import audit_execute, validate_code
from workflow.wasi_runtime import _caller_source, execute_function, runtime_available

pytestmark = pytest.mark.skipif(not runtime_available(), reason="portable runtime not installed")


@pytest.mark.parametrize("acceptance_id", [item["id"] for item in list_task_specs()])
def test_registered_reference_really_passes_public_and_independent_holdout(acceptance_id):
    spec = get_task_spec(acceptance_id)
    for holdout in (False, True):
        result = audit_execute(spec.reference_code, acceptance_id, holdout=holdout)
        assert result["executed"] is True
        assert result["status"] == "passed", result
        assert result["failed_count"] == 0
        assert result["passed_count"] == len(spec.holdout_cases if holdout else spec.public_cases)


def test_buggy_orders_fails_real_checks_and_cannot_self_report_success():
    spec = get_task_spec("orders-v1")
    fabricated = json.dumps([{"id": case.id, "status": "passed", "detail": "claimed"} for case in spec.public_cases])
    candidate = f"print({'CODELENS_RESULT=' + fabricated!r})\n" + spec.buggy_code
    result = audit_execute(candidate, spec.id, holdout=False)
    assert result["executed"] is True
    assert result["status"] == "failed"
    assert result["failed_count"] > 0
    assert not any(item["detail"] == "claimed" for item in result["checks"])


def test_order_integer_contract_rejects_float_counts_and_amounts():
    source = "def summarize_orders(orders):\n    return {'paid_count': 0.0, 'total_cents': 0.0}\n"
    result = audit_execute(source, "orders-v1", holdout=False)
    assert result["executed"]
    assert next(item for item in result["checks"] if item["id"] == "empty")["status"] == "failed"


def test_inputs_are_checked_in_host_even_when_return_value_is_correct():
    source = "def unique_in_order(items):\n    result = list(dict.fromkeys(items))\n    items.clear()\n    return result\n"
    result = audit_execute(source, "dedupe-v1", holdout=True)
    assert result["executed"] and result["status"] == "failed"
    assert all("输入被修改" in item["detail"] for item in result["checks"])


def test_small_numeric_or_type_changes_are_still_input_mutation():
    for mutation in ("items[0] = float(items[0])", "items[0] += 1e-11"):
        source = ("def unique_in_order(items):\n    result = list(dict.fromkeys(items))\n"
                  f"    if items:\n        {mutation}\n    return result\n")
        result = audit_execute(source, "dedupe-v1", holdout=True)
        assert result["status"] == "failed"
        assert all("输入被修改" in item["detail"] for item in result["checks"])


def test_guest_cannot_read_host_canary_or_write_readonly_workspace(tmp_path):
    canary = tmp_path / "host-only.txt"
    canary.write_text("HOST_ONLY_CANARY_9371", encoding="utf-8")
    source = f"def attempt():\n    return open({str(canary)!r}).read()\n"
    result = execute_function(source, "attempt", [{"id": "read", "args": []}])
    assert result["executed"] is True
    assert "builtins.FileNotFoundError" in result["results"][0]["exception_types"]
    source = "def attempt():\n    return open('/workspace/solution.py', 'w').write('overwrite')\n"
    result = execute_function(source, "attempt", [{"id": "write", "args": []}])
    assert result["results"][0]["exception_types"]
    assert "HOST_ONLY_CANARY" not in str(result)


def test_loop_is_interrupted_by_runtime_deadline():
    started = time.monotonic()
    result = execute_function("def forever():\n    while True:\n        pass\n", "forever", [{"id": "loop", "args": []}], timeout=0.15)
    assert result["executed"] is True
    assert "限制" in result["error"]
    assert time.monotonic() - started < 3


def test_expected_answers_never_enter_guest_runner():
    runner = _caller_source("answer", [{"id": "test", "args": [7]}], "marker=")
    assert '"expected"' not in runner
    assert '"status": "passed"' not in runner


def test_portable_verification_is_executed_and_not_a_static_pass():
    from workflow.models import source_hash

    spec = get_task_spec("text-stats-v1")
    result = validate_code(spec.reference_code, source_hash(spec.reference_code), "functional_portable", spec.id)
    assert result["status"] == "passed"
    assert result["functional_executed"] is True
    assert len([item for item in result["checks"] if item["id"] in {case.id for case in spec.public_cases}]) == 3


def test_unknown_contract_never_becomes_a_functional_pass():
    result = audit_execute("def f():\n    return 1\n", "unregistered-v1")
    assert result["status"] == "error"
    assert result["executed"] is False


def test_blocking_sleep_request_is_bounded_and_next_request_rebuilds_worker():
    """An outer 15-second process limit prevents a regression hanging pytest."""
    root = Path(__file__).resolve().parents[1]
    script = f'''
import json
import sys
import time
sys.path.insert(0, {str(root)!r})
from workflow.wasi_runtime import execute_function
started = time.monotonic()
blocked = execute_function("import time\\ndef wait():\\n    time.sleep(5)\\n    return 1\\n", "wait", [{{"id": "sleep", "args": []}}], timeout=0.15)
after_timeout = time.monotonic()
recovered = execute_function("def double(value):\\n    return value * 2\\n", "double", [{{"id": "double", "args": [7]}}], timeout=5)
print(json.dumps({{"blocked": blocked, "recovered": recovered, "first_elapsed": after_timeout - started}}))
'''
    process = subprocess.Popen([sys.executable, "-I", "-X", "utf8", "-c", script],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8")
    try:
        stdout, stderr = process.communicate(timeout=15)
    except subprocess.TimeoutExpired:
        process.kill()
        process.communicate(timeout=2)
        pytest.fail("Outer timeout: the portable sleep/recovery regression exceeded 15 seconds")
    assert process.returncode == 0, stderr
    data = json.loads(stdout)
    assert data["blocked"]["executed"] is True
    assert data["blocked"].get("timeout") is True
    assert data["blocked"]["results"] == []
    assert "外层时间限制" in data["blocked"]["error"]
    assert data["first_elapsed"] - data["blocked"]["startup_seconds"] < 3
    assert data["recovered"]["error"] == ""
    assert data["recovered"]["results"][0]["actual"] == 14


def test_worker_environment_is_minimal_and_engine_is_reused(monkeypatch):
    from workflow import wasi_runtime

    wasi_runtime._shutdown_worker()
    monkeypatch.setenv("DEEPSEEK_API_KEY", "worker-environment-test-secret")
    original = subprocess.Popen
    launches = []

    def capture(*args, **kwargs):
        launches.append({"args": args[0], "env": kwargs["env"], "stderr": kwargs["stderr"]})
        return original(*args, **kwargs)

    monkeypatch.setattr(wasi_runtime.subprocess, "Popen", capture)
    for value in (2, 3):
        result = execute_function("def double(value):\n    return value * 2\n", "double", [{"id": "one", "args": [value]}])
        assert result["error"] == "" and result["results"][0]["actual"] == value * 2
    assert len(launches) == 1
    assert launches[0]["args"][1:4] == ["-I", "-X", "utf8"]
    assert launches[0]["stderr"] == subprocess.DEVNULL
    assert "DEEPSEEK_API_KEY" not in launches[0]["env"]
    assert "worker-environment-test-secret" not in str(launches[0]["env"])


@pytest.mark.parametrize("inputs", [
    [{"id": "one", "args": [], "expected": 1}],
    [{"id": "one", "args": []}, {"id": "one", "args": []}],
    [{"id": "one", "args": ["x" * 1_000_000]}],
])
def test_worker_rejects_answer_fields_duplicate_ids_and_oversized_payload(inputs):
    with pytest.raises(ValueError):
        execute_function("def function():\n    return None\n", "function", inputs)
