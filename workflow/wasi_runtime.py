"""Run CPython bytecode inside Wasmtime, with only read-only explicit mounts.

No submitted source is imported or executed by the host Python interpreter.
The pinned runtime archive ships with the project; no download occurs at run time.
"""

import atexit
import hashlib
import json
import math
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from functools import lru_cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARCHIVE_NAME = "python-3.13.15-wasi_sdk-24.zip"
ARCHIVE_SHA256 = "67e1c32a85d5e0600c0939f5fd4023ca0127f1a9e81f5499697467bda67af78d"
OUTPUT_LIMIT = 256_000
_LOCK = threading.Lock()
_WORKER_LOCK = threading.Lock()
_WORKER = None
STARTUP_TIMEOUT = 20.0
REQUEST_LIMIT = 1_000_000
RESPONSE_LIMIT = 2_000_000


def _prepare_runtime() -> Path:
    archive = ROOT / "sandbox_runtime" / ARCHIVE_NAME
    if not archive.is_file() or hashlib.sha256(archive.read_bytes()).hexdigest() != ARCHIVE_SHA256:
        raise RuntimeError("便携解释器缺失或校验失败，请使用完整发行包。")
    destination = ROOT / "data" / "wasi-runtime" / ARCHIVE_SHA256[:12]
    marker = destination / ".ready"
    if marker.is_file() and marker.read_text(encoding="ascii") == ARCHIVE_SHA256:
        return destination
    destination.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as source:
        if sum(item.file_size for item in source.infolist()) > 64_000_000:
            raise RuntimeError("解释器归档超出允许大小。")
        for item in source.infolist():
            target = (destination / item.filename).resolve()
            if not target.is_relative_to(destination.resolve()):
                raise RuntimeError("解释器归档包含非法路径。")
        source.extractall(destination)
    marker.write_text(ARCHIVE_SHA256, encoding="ascii")
    return destination


@lru_cache(maxsize=1)
def _engine_module():
    import wasmtime

    with _LOCK:
        runtime = _prepare_runtime()
        config = wasmtime.Config()
        config.consume_fuel = True
        config.epoch_interruption = True
        engine = wasmtime.Engine(config)
        module = wasmtime.Module.from_file(engine, str(runtime / "python.wasm"))

        def tick():
            waiter = threading.Event()
            while not waiter.wait(0.05):
                engine.increment_epoch()

        threading.Thread(target=tick, name="codelens-wasi-deadline", daemon=True).start()
        return engine, module, runtime


def runtime_available() -> bool:
    try:
        import wasmtime  # noqa: F401
        return (ROOT / "sandbox_runtime" / ARCHIVE_NAME).is_file()
    except ImportError:
        return False


def _caller_source(entrypoint: str, inputs: list[dict], marker: str) -> str:
    """Expected answers stay in the host, outside the guest's filesystem."""
    payload = json.dumps(inputs, ensure_ascii=False, allow_nan=False)
    return f'''import copy
import json
import runpy

_inputs = json.loads({payload!r})
_results = []
_write_result = json.dumps
try:
    _namespace = runpy.run_path("/workspace/solution.py")
    _function = _namespace[{entrypoint!r}]
    for _case in _inputs:
        _args = copy.deepcopy(_case["args"])
        try:
            _actual = _function(*_args)
            _item = {{"id": _case["id"], "actual": _actual, "exception_types": [], "input_after": _args}}
        except BaseException as _exc:
            _types = [t.__module__ + "." + t.__name__ for t in type(_exc).__mro__]
            _item = {{"id": _case["id"], "actual": None, "exception_types": _types, "input_after": _args}}
        try:
            _write_result(_item, allow_nan=False)
        except BaseException:
            _item = {{"id": _case["id"], "protocol_error": "result is not finite JSON data"}}
        _results.append(_item)
except BaseException as _load_error:
    _results = [{{"id": _case["id"], "protocol_error": "load: " + type(_load_error).__name__}} for _case in _inputs]
print({marker!r} + _write_result(_results, ensure_ascii=False, allow_nan=False))
'''


def _internal_execute_function(code: str, entrypoint: str, inputs: list[dict], *, timeout: float = 10) -> dict:
    """Return actual values, never a candidate-supplied pass/fail verdict."""
    if not entrypoint.isidentifier() or not 0 < len(inputs) <= 100:
        raise ValueError("非法函数入口或用例数量。")
    if len(code.encode("utf-8")) > 48_000:
        raise ValueError("源码超出隔离验收大小限制。")
    if not runtime_available():
        return {"executed": False, "error": "便携隔离运行时不可用，请安装 wasmtime 并使用完整发行包。", "results": []}
    import wasmtime

    stdout, stderr = bytearray(), bytearray()
    overflow = [False]

    def capture(buffer):
        def write(data):
            remaining = OUTPUT_LIMIT - len(buffer)
            buffer.extend(data[:max(0, remaining)])
            if len(data) > remaining:
                overflow[0] = True
                return -1
            return len(data)
        return write

    try:
        engine, module, runtime = _engine_module()
    except (ImportError, OSError, RuntimeError, ValueError, wasmtime.WasmtimeError) as error:
        return {"executed": False, "error": f"隔离解释器初始化失败：{type(error).__name__}", "results": []}
    marker = "CODELENS_ACTUAL_" + uuid.uuid4().hex + "="
    temporary_root = ROOT / "data" / "sandbox-work"
    temporary_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="codelens-wasi-", dir=temporary_root) as temporary:
        work = Path(temporary)
        (work / "solution.py").write_text(code, encoding="utf-8")
        (work / "runner.py").write_text(_caller_source(entrypoint, inputs, marker), encoding="utf-8")
        with wasmtime.Store(engine) as store:
            store.set_limits(memory_size=256 * 1024 * 1024, instances=2, memories=1)
            store.set_fuel(2_000_000_000)
            store.set_epoch_deadline(max(1, math.ceil(max(0.1, timeout) / 0.05)))
            wasi = wasmtime.WasiConfig()
            wasi.argv = ["/python/python.wasm", "-B", "-s", "-P", "/workspace/runner.py"]
            wasi.env = [("PYTHONHOME", "/python"), ("PYTHONUTF8", "1")]
            wasi.preopen_dir(str(runtime), "/python", fs_mutable=False)
            wasi.preopen_dir(str(work), "/workspace", fs_mutable=False)
            wasi.stdout_custom = capture(stdout)
            wasi.stderr_custom = capture(stderr)
            store.set_wasi(wasi)
            with wasmtime.Linker(engine) as linker:
                linker.define_wasi()
                try:
                    instance = linker.instantiate(store, module)
                    instance.exports(store)["_start"](store)
                except wasmtime.ExitTrap as error:
                    if error.code:
                        return {"executed": True, "error": f"隔离程序异常退出（{error.code}）。", "results": []}
                except wasmtime.Trap:
                    return {"executed": True, "error": "隔离程序达到时间、内存或指令限制。", "results": []}
                except wasmtime.WasmtimeError:
                    return {"executed": False, "error": "隔离运行时无法启动。", "results": []}
    if overflow[0]:
        return {"executed": True, "error": "隔离程序输出超出上限。", "results": []}
    for line in reversed(stdout.decode("utf-8", errors="replace").splitlines()):
        if line.startswith(marker):
            try:
                results = json.loads(line[len(marker):])
                ids = [case["id"] for case in inputs]
                if not isinstance(results, list) or len(results) != len(ids):
                    break
                if any(not isinstance(item, dict) for item in results) or [item.get("id") for item in results] != ids:
                    break
                return {"executed": True, "error": "", "results": results, "backend": "cpython-wasi",
                        "runtime_sha256": ARCHIVE_SHA256}
            except (ValueError, TypeError):
                break
    return {"executed": True, "error": "隔离程序没有返回完整的实际执行结果。", "results": []}


class _TrustedWorker:
    """The child runs only the shipped wrapper; guest output is collected inside Wasmtime."""

    def __init__(self):
        allowed = {"SYSTEMROOT", "WINDIR", "TEMP", "TMP", "PROCESSOR_ARCHITECTURE", "PROCESSOR_ARCHITEW6432"}
        environment = {key: value for key, value in os.environ.items() if key.upper() in allowed}
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
        self.process = subprocess.Popen(
            [sys.executable, "-I", "-X", "utf8", str(Path(__file__).with_name("wasi_worker.py"))],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            cwd=str(ROOT), env=environment, creationflags=flags,
        )
        self.messages = queue.Queue(maxsize=3)
        self.reader = threading.Thread(target=self._read, name="codelens-wasi-protocol", daemon=True)
        self.reader.start()

    def _put(self, item):
        try:
            self.messages.put_nowait(item)
        except queue.Full:
            pass

    def _read(self):
        try:
            while True:
                line = self.process.stdout.readline(RESPONSE_LIMIT + 1)
                if not line:
                    self._put((False, "隔离工作进程已退出。"))
                    return
                if len(line) > RESPONSE_LIMIT or not line.endswith(b"\n"):
                    self._put((False, "隔离工作进程响应超出协议上限。"))
                    return
                try:
                    value = json.loads(line.decode("utf-8"))
                except (UnicodeError, ValueError, RecursionError):
                    self._put((False, "隔离工作进程响应格式无效。"))
                    return
                self._put((True, value))
        except (OSError, ValueError):
            self._put((False, "隔离工作进程通信中断。"))

    def receive(self, timeout):
        return self.messages.get(timeout=timeout)

    def send(self, payload: bytes):
        def write():
            try:
                self.process.stdin.write(payload + b"\n")
                self.process.stdin.flush()
            except (OSError, ValueError):
                self._put((False, "无法向隔离工作进程发送请求。"))
        # Writing a bounded message cannot stall the caller past its request deadline.
        threading.Thread(target=write, name="codelens-wasi-request", daemon=True).start()

    def close(self):
        try:
            if self.process.poll() is None:
                self.process.kill()
            self.process.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        self.reader.join(timeout=0.3)
        for pipe in (self.process.stdin, self.process.stdout):
            try:
                pipe.close()
            except (OSError, ValueError):
                pass


def _shutdown_worker():
    global _WORKER
    worker, _WORKER = _WORKER, None
    if worker is not None:
        worker.close()


atexit.register(_shutdown_worker)


def execute_function(code: str, entrypoint: str, inputs: list[dict], *, timeout: float = 10) -> dict:
    """Run a request with a host-enforced deadline, including blocking WASI calls.

    A cold trusted worker has a separate 20-second startup allowance. After its
    ready handshake, the request deadline covers both communication and execution.
    The workflow's outer activity metric still includes startup and execution.
    """
    global _WORKER
    if not isinstance(code, str) or len(code.encode("utf-8")) > 48_000:
        raise ValueError("源码超出隔离验收大小限制。")
    if not isinstance(entrypoint, str) or not entrypoint.isidentifier() or not isinstance(inputs, list) or not 0 < len(inputs) <= 100:
        raise ValueError("非法函数入口或用例数量。")
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or not 0 < timeout <= 60:
        raise ValueError("隔离请求超时必须为 0～60 秒之间的有限正数。")
    if any(not isinstance(item, dict) or not isinstance(item.get("id"), str) or not isinstance(item.get("args"), list)
           or set(item) != {"id", "args"} for item in inputs):
        raise ValueError("隔离输入只允许 id 和 args，不接受期望答案或判定字段。")
    if len({item["id"] for item in inputs}) != len(inputs):
        raise ValueError("隔离输入用例 ID 必须唯一。")
    request_id = uuid.uuid4().hex
    payload = json.dumps({"protocol": 1, "request_id": request_id, "code": code, "entrypoint": entrypoint,
                          "inputs": inputs, "timeout": timeout}, ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(payload) + 1 > REQUEST_LIMIT:
        raise ValueError("隔离请求超出通信大小限制。")
    if not runtime_available():
        return {"executed": False, "error": "便携隔离运行时不可用，请安装 wasmtime 并使用完整发行包。", "results": []}
    with _WORKER_LOCK:
        startup_seconds = 0.0
        if _WORKER is None or _WORKER.process.poll() is not None:
            _shutdown_worker()
            started = time.monotonic()
            try:
                _WORKER = _TrustedWorker()
                valid, ready = _WORKER.receive(STARTUP_TIMEOUT)
                if not valid or not isinstance(ready, dict) or ready.get("kind") != "ready" or ready.get("protocol") != 1:
                    _shutdown_worker()
                    return {"executed": False, "error": "便携解释器工作进程初始化失败。", "results": []}
            except queue.Empty:
                _shutdown_worker()
                return {"executed": False, "error": "便携解释器初始化超过 20 秒，已终止工作进程。", "results": []}
            except OSError:
                _shutdown_worker()
                return {"executed": False, "error": "无法启动便携解释器工作进程。", "results": []}
            startup_seconds = time.monotonic() - started
        _WORKER.send(payload)
        try:
            valid, response = _WORKER.receive(float(timeout))
        except queue.Empty:
            _shutdown_worker()
            return {"executed": True, "error": "隔离请求达到外层时间限制，工作进程已终止；下次请求会重建。",
                    "results": [], "timeout": True, "startup_seconds": startup_seconds}
        if (not valid or not isinstance(response, dict) or response.get("kind") != "result"
                or response.get("protocol") != 1 or response.get("request_id") != request_id
                or not isinstance(response.get("result"), dict)):
            _shutdown_worker()
            return {"executed": False, "error": "隔离工作进程未返回对应请求的可信协议结果。", "results": []}
        result = response["result"]
        result["startup_seconds"] = startup_seconds
        return result
