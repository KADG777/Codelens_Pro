"""Deterministic checks; submitted Python executes only in explicit sandboxes."""

import ast
import copy
import json
import math
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path

from review_agent.tools import ruff_checks
from workflow.examples import ORDER_HARNESS
from workflow.registry import get_task_spec

DOCKER_IMAGE = "python:3.11-slim"


def validate_configuration(profile: str, acceptance_id: str) -> None:
    """Reject immutable configuration faults before creating or resuming a run."""
    if profile not in {"static_only", "functional_required", "functional_portable"}:
        raise ValueError("未知验证档位。")
    if profile != "static_only" and acceptance_id == "none":
        raise ValueError("功能验收需要绑定验收合同。请先加载与代码匹配的任务合同，或选择静态检查后重新开始。")
    if profile == "functional_required" and acceptance_id != "orders-v1":
        raise ValueError("Docker 验收仅支持订单合同；其他已注册合同请选择便携功能验收。")
    if acceptance_id != "none":
        get_task_spec(acceptance_id)


def _check(name: str, status: str, detail: str) -> dict:
    return {"id": name, "name": name, "status": status, "detail": detail}


def _same_json(actual, expected) -> bool:
    if isinstance(expected, bool) or expected is None:
        return actual is expected
    if isinstance(expected, (int, float)):
        if type(expected) is int:
            return type(actual) is int and actual == expected
        try:
            return (type(actual) in {int, float} and math.isfinite(actual)
                    and math.isclose(actual, expected, rel_tol=1e-10, abs_tol=1e-12))
        except (OverflowError, ValueError):
            return False
    if isinstance(expected, list):
        return (isinstance(actual, list) and len(actual) == len(expected)
                and all(_same_json(a, b) for a, b in zip(actual, expected)))
    if isinstance(expected, dict):
        return (isinstance(actual, dict) and actual.keys() == expected.keys()
                and all(_same_json(actual[key], value) for key, value in expected.items()))
    return type(actual) is type(expected) and actual == expected


def _same_input(actual, expected) -> bool:
    """Input preservation is exact, even when numeric answers use tolerance."""
    if type(actual) is not type(expected):
        return False
    if isinstance(expected, list):
        return len(actual) == len(expected) and all(_same_input(a, b) for a, b in zip(actual, expected))
    if isinstance(expected, dict):
        return actual.keys() == expected.keys() and all(_same_input(actual[key], value) for key, value in expected.items())
    return actual == expected


def audit_execute(code: str, acceptance_id: str, *, holdout: bool = True, timeout: float = 10) -> dict:
    """Compare actual sandbox values against expected answers held by the host.

    Held-out checks are evaluator-only; the workflow calls this with holdout=False.
    No pass/fail verdict from candidate stdout is accepted.
    """
    from workflow.wasi_runtime import execute_function

    try:
        spec = get_task_spec(acceptance_id)
    except ValueError as error:
        return {"status": "error", "executed": False, "checks": [], "passed_count": 0,
                "failed_count": 0, "error": str(error)}
    cases = spec.holdout_cases if holdout else spec.public_cases
    if not cases:
        return {"status": "error", "executed": False, "checks": [], "passed_count": 0,
                "failed_count": 0, "error": "该任务没有对应的冻结验收用例。"}
    inputs = [{"id": case.id, "args": copy.deepcopy(list(case.args))} for case in cases]
    result = execute_function(code, spec.entrypoint, inputs, timeout=timeout)
    if not result.get("error"):
        actual_results = result.get("results")
        valid_protocol = (isinstance(actual_results, list) and len(actual_results) == len(cases)
                          and all(isinstance(item, dict) for item in actual_results)
                          and [item.get("id") for item in actual_results] == [case.id for case in cases])
        if not valid_protocol:
            result["error"] = "隔离程序返回的用例集合不完整或不匹配。"
    checks = []
    if result.get("error"):
        status = "failed" if result.get("executed") else "error"
        checks.append(_check("functional", status, result["error"]))
    else:
        for case, returned in zip(cases, result["results"]):
            protocol_error = returned.get("protocol_error")
            exceptions = returned.get("exception_types")
            complete = all(key in returned for key in ("actual", "exception_types", "input_after"))
            if protocol_error or not complete or not isinstance(exceptions, list):
                passed, detail = False, str(protocol_error or "执行结果合同无效。")
            else:
                input_preserved = not case.preserve_input or _same_input(returned["input_after"], list(case.args))
                correct = ("builtins." + case.raises in exceptions) if case.raises else (
                    not exceptions and _same_json(returned["actual"], case.expected))
                passed = correct and input_preserved
                detail = ("抛出 " + str(exceptions[0]) if exceptions else "实际返回 " + repr(returned["actual"])[:300])
                if not input_preserved:
                    detail += "；输入被修改"
            checks.append({**_check(case.id, "passed" if passed else "failed", detail),
                           "expected": case.raises or repr(case.expected), "actual": returned.get("actual")})
    passed_count = sum(item["status"] == "passed" for item in checks)
    failed_count = sum(item["status"] == "failed" for item in checks)
    status = "error" if not result.get("executed") else ("failed" if failed_count else "passed")
    return {"status": status, "executed": bool(result.get("executed")), "checks": checks,
            "passed_count": passed_count, "failed_count": failed_count, "error": result.get("error", ""),
            "backend": "cpython-wasi", "holdout": holdout,
            "runtime_sha256": result.get("runtime_sha256", "")}


def docker_checks(code: str, *, timeout: float = 10) -> tuple[list[dict], bool]:
    executable = shutil.which("docker")
    if not executable:
        return [_check("docker", "error", "未发现 Docker。请配置 Docker 或新建静态验证运行。")], False
    try:
        probe = subprocess.run([executable, "image", "inspect", DOCKER_IMAGE], capture_output=True,
                               timeout=min(5, timeout), shell=False)
        if probe.returncode:
            return [_check("docker", "error", f"Docker 服务或本地镜像 {DOCKER_IMAGE} 不可用；不自动拉取镜像。")], False
    except (OSError, subprocess.TimeoutExpired):
        return [_check("docker", "error", "Docker 环境检查失败或超时。")], False
    container = "codelens-" + uuid.uuid4().hex
    with tempfile.TemporaryDirectory(prefix="codelens-flow-") as directory:
        workspace = Path(directory)
        (workspace / "solution.py").write_text(code, encoding="utf-8")
        (workspace / "runner.py").write_text(ORDER_HARNESS, encoding="utf-8")
        workspace.chmod(0o755)
        (workspace / "solution.py").chmod(0o644)
        (workspace / "runner.py").chmod(0o644)
        command = [executable, "run", "--rm", "--name", container, "--network", "none",
                   "--read-only", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                   "--pids-limit", "64", "--memory", "128m", "--cpus", "0.5",
                   "--user", "65534:65534", "--tmpfs", "/tmp:rw,noexec,nosuid,size=16m",
                   "--mount", f"type=bind,source={workspace.resolve()},target=/workspace,readonly",
                   "--workdir", "/workspace", DOCKER_IMAGE, "python", "-I", "-B", "runner.py"]
        try:
            # Candidate output goes to bounded temporary files, never into an unbounded pipe.
            with tempfile.TemporaryFile() as output, tempfile.TemporaryFile() as errors:
                result = subprocess.run(command, stdout=output, stderr=errors, timeout=timeout, shell=False)
                output.seek(0)
                stdout = output.read(256_000).decode("utf-8", errors="replace")
            if result.returncode == 125:
                return [_check("docker", "error", "容器无法启动，请检查运行环境。")], False
            if result.returncode:
                return [_check("functional", "failed", "候选执行异常退出，不能判定功能通过。")], True
            for line in reversed(stdout.splitlines()):
                if line.startswith("CODELENS_RESULT="):
                    checks = json.loads(line.removeprefix("CODELENS_RESULT="))
                    expected_ids = {"empty", "mixed", "zero", "missing", "negative", "bool_amount", "bool_paid", "bad_order"}
                    if (not isinstance(checks, list) or len(checks) != len(expected_ids)
                            or any(not isinstance(item, dict) or item.get("status") not in {"passed", "failed"}
                                   or not isinstance(item.get("detail"), str) for item in checks)
                            or {item.get("id") for item in checks} != expected_ids):
                        break
                    return checks, True
            return [_check("functional", "failed", "候选代码执行失败或未返回测试结果。")], True
        except subprocess.TimeoutExpired:
            return [_check("functional", "failed", "候选代码超过功能测试时间限制，已终止容器。")], True
        except (OSError, ValueError, TypeError):
            return [_check("docker", "error", "无法读取容器验证结果。")], False
        finally:
            try:
                subprocess.run([executable, "rm", "-f", container], capture_output=True, timeout=5, shell=False)
            except (OSError, subprocess.TimeoutExpired):
                pass


def validate_code(code: str, code_hash: str, profile: str, acceptance_id: str,
                  *, timeout: float = 10) -> dict:
    checks = []
    tree = None
    try:
        tree = ast.parse(code)
        checks.append(_check("syntax", "passed", "Python AST 解析通过；未在宿主导入或执行代码。"))
    except (SyntaxError, ValueError, RecursionError) as exc:
        checks.append(_check("syntax", "failed", f"Python 语法检查失败：{type(exc).__name__}"))
    ruff = ruff_checks(code)
    if ruff["status"] != "completed":
        checks.append(_check("ruff", "error", ruff.get("detail", "Ruff 检查不可用。")))
    else:
        blockers = [item for item in ruff["findings"] if item["severity"] in {"high", "medium"}]
        checks.append(_check("ruff", "failed" if blockers else "passed",
                             "; ".join(item["title"] for item in blockers) or "配置的阻断静态规则通过。"))
    if acceptance_id != "none" and tree is not None:
        try:
            spec = get_task_spec(acceptance_id)
            matches = [node for node in tree.body if isinstance(node, ast.FunctionDef)
                       and node.name == spec.entrypoint]
            valid = len(matches) == 1 and len(matches[0].args.args) == len(spec.parameters)
            signature = f"{spec.entrypoint}({', '.join(spec.parameters)})"
            checks.append(_check("interface", "passed" if valid else "failed", f"要求 {signature} 函数。"))
        except ValueError as error:
            checks.append(_check("interface", "error", str(error)))
    functional_executed = False
    execution_backend, runtime_sha256 = "", ""
    if profile == "functional_portable":
        if acceptance_id == "none":
            checks.append(_check("functional", "error", "请先选择一个已注册的任务验收规格。"))
        elif any(item["status"] in {"failed", "error"} for item in checks):
            checks.append(_check("functional", "skipped", "静态前置检查未通过，暂不执行功能用例。"))
        else:
            execution = audit_execute(code, acceptance_id, holdout=False, timeout=timeout)
            functional_executed = execution["executed"]
            execution_backend = execution.get("backend", "")
            runtime_sha256 = execution.get("runtime_sha256", "")
            checks.extend(execution["checks"])
    elif profile == "functional_required":
        if acceptance_id != "orders-v1":
            checks.append(_check("functional", "error", "首版仅提供内置订单合同的容器验收；此任务没有冻结功能用例。"))
        elif any(item["status"] in {"failed", "error"} for item in checks):
            checks.append(_check("functional", "skipped", "静态前置检查未通过，暂不执行功能用例。"))
        else:
            functional, functional_executed = docker_checks(code, timeout=timeout)
            execution_backend = "docker"
            checks.extend(functional)
    else:
        checks.append(_check("functional", "skipped", "static_only：未执行功能测试。"))
    status = "error" if any(item["status"] == "error" for item in checks) else (
        "failed" if any(item["status"] == "failed" for item in checks) else "passed")
    return {"code_hash": code_hash, "status": status, "checks": checks,
            "functional_executed": functional_executed,
            "backend": execution_backend, "runtime_sha256": runtime_sha256,
            "passed_count": sum(item["status"] == "passed" for item in checks),
            "failed_count": sum(item["status"] == "failed" for item in checks)}
