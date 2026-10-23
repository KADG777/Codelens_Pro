"""Trusted persistent transport. Submitted code is passed only to Wasmtime."""

import importlib.util
import json
import math
import sys
from pathlib import Path

REQUEST_LIMIT = 1_000_000
RESPONSE_LIMIT = 2_000_000


def emit(value):
    encoded = json.dumps(value, ensure_ascii=False, allow_nan=False)
    if len(encoded.encode("utf-8")) > RESPONSE_LIMIT - 1:
        raise ValueError("worker response too large")
    sys.stdout.write(encoded + "\n")
    sys.stdout.flush()


def main():
    # Load this one trusted sibling without importing application UI or dotenv code.
    specification = importlib.util.spec_from_file_location("codelens_wasi_backend", Path(__file__).with_name("wasi_runtime.py"))
    backend = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(backend)
    try:
        backend._engine_module()
    except BaseException as error:
        emit({"protocol": 1, "kind": "startup_error", "error_type": type(error).__name__, "detail": str(error)[:400]})
        return 1
    emit({"protocol": 1, "kind": "ready"})
    while True:
        line = sys.stdin.buffer.readline(REQUEST_LIMIT + 1)
        if not line:
            return 0
        if len(line) > REQUEST_LIMIT or not line.endswith(b"\n"):
            return 2
        request_id = ""
        try:
            request = json.loads(line.decode("utf-8"))
            request_id = request["request_id"]
            timeout = request["timeout"]
            if (request.get("protocol") != 1 or not isinstance(request_id, str)
                    or type(timeout) not in {int, float} or not math.isfinite(timeout) or not 0 < timeout <= 60):
                raise ValueError("invalid request")
            # The parent deadline is authoritative and includes IPC. Epoch remains
            # a secondary guard with a short grace, avoiding two racing cutoffs.
            inner_timeout = min(60, max(0.5, timeout + 0.25))
            result = backend._internal_execute_function(request["code"], request["entrypoint"], request["inputs"], timeout=inner_timeout)
        except BaseException as error:
            result = {"executed": False, "error": "隔离工作进程请求失败：" + type(error).__name__, "results": []}
        try:
            emit({"protocol": 1, "kind": "result", "request_id": request_id, "result": result})
        except BaseException:
            return 3


if __name__ == "__main__":
    raise SystemExit(main())
