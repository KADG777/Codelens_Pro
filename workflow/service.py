"""UI-independent run service. One committed graph state owns source and results."""

import copy
import json
import sqlite3
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from review_agent.llm import ModelError
from workflow import agents
from workflow.config import WorkflowConfig
from workflow.examples import ORDER_TASK
from workflow.models import (
    WorkflowState,
    artifact,
    current_artifact,
    meaningful_source_change,
    normalize_source,
    public_interfaces,
    source_hash,
)
from workflow.node_registry import CORE_NODES, default_registry
from workflow.registry import get_acceptance_hash, get_task_spec
from workflow.tracing import handoff
from workflow.validation import validate_code, validate_configuration

_LOCKS: dict[tuple[str, str], threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()
_EXECUTING: set[tuple[str, str]] = set()
TERMINAL = {"completed_static", "completed_verified", "needs_attention", "failed"}
DEFAULT_LIMITS = {"max_model_calls": 30, "max_tokens": 100_000, "max_active_seconds": 300.0}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def event(data: dict, node: str, status: str, message: str) -> None:
    data["events"].append({"id": uuid.uuid4().hex, "time": utc_now(), "node": node,
                           "status": status, "message": message, "version": data["current_version"]})


class WorkflowRunService:
    """Local synchronous service; browser reruns never own workflow state.

    Credentials and model clients are runtime-only. Code snapshots and their current
    pointer are committed together by LangGraph's SQLite checkpointer. A recoverable
    fault enters a real interrupt node; resume retries only the failed node.
    """

    def __init__(self, db_path: str | Path, model_factory=None, validation_runner=None, node_registry=None):
        self.db_path = str(Path(db_path).resolve())
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.saver = SqliteSaver(self.connection)
        self.saver.setup()
        self.model_factory = model_factory
        self.validation_runner = validation_runner or validate_code
        self.node_registry = node_registry if node_registry is not None else default_registry()

    @staticmethod
    def _config(run_id: str) -> dict:
        return {"configurable": {"thread_id": run_id}, "recursion_limit": 80}

    def _graph(self, settings=None, on_event=None):
        graph = StateGraph(WorkflowState)
        graph.add_node("dispatch", lambda state: state)
        node_names = [*CORE_NODES, *(spec.id for spec in self.node_registry.all())]
        for name in node_names:
            graph.add_node(name, self._node(name, settings, on_event))
        from workflow.canvas_runtime import step
        graph.add_node("canvas", lambda state: step(self, state, settings, on_event))
        node_names.append("canvas")

        def pause(state):
            data = copy.deepcopy(state["data"])
            interrupt({"node": data["next_node"], "error": data["error"], "recoverable": True})
            data.update(status="running", recoverable=False, error="", termination_reason="")
            event(data, "resume", "running", f"恢复至 {data['next_node']}，延续原预算与版本。")
            return {"data": data}

        graph.add_node("pause", pause)
        graph.add_edge(START, "dispatch")
        destinations = [*node_names, "pause", END]

        def route(state):
            data = state["data"]
            if data["status"] in TERMINAL:
                return END
            if data["status"] == "blocked":
                return "pause"
            return data["next_node"]

        for name in ("dispatch", *node_names, "pause"):
            graph.add_conditional_edges(name, route, destinations)
        return graph.compile(checkpointer=self.saver)

    def create_run(self, task: str, source: str | None = None, filename: str = "solution.py",
                   mode: str = "demo", max_fix_rounds: int = 2,
                   validation_profile: str = "static_only", scenario: str = "orders",
                   budget_limits: dict | None = None, max_model_calls: int | None = None,
                   active_timeout: float | None = None, parent_run_id: str | None = None,
                   acceptance_id: str | None = None, workflow_config: dict | WorkflowConfig | None = None) -> str:
        if not isinstance(task, str) or not task.strip() or len(task) > 8000:
            raise ValueError("请填写 1～8000 字符的任务或审查目标。")
        if mode not in {"demo", "live"}:
            raise ValueError("mode 必须是 demo 或 live。")
        if validation_profile not in {"static_only", "functional_required", "functional_portable"}:
            raise ValueError("未知验证档位。")
        if scenario not in {"orders", "clean", "unresolved"}:
            raise ValueError("未知预设情景。")
        if isinstance(max_fix_rounds, bool) or not isinstance(max_fix_rounds, int) or not 0 <= max_fix_rounds <= 2:
            raise ValueError("首版最多允许 0～2 轮业务修复。")
        if workflow_config is not None and not isinstance(workflow_config, (dict, WorkflowConfig)):
            raise ValueError("工作流配置必须是结构化对象。")
        config_data = workflow_config.model_dump() if isinstance(workflow_config, WorkflowConfig) else (workflow_config or {})
        plan = WorkflowConfig.model_validate({"max_fix_rounds": max_fix_rounds, **config_data})
        if plan.canvas:
            plan.canvas.validate_for(self.node_registry)
            plan.extension_nodes = plan.canvas.extensions()
        extension_manifest = self.node_registry.manifest(plan.extension_nodes)
        filename = filename.replace("\\", "/").rsplit("/", 1)[-1]
        if not filename.lower().endswith(".py"):
            raise ValueError("工作流仅支持 .py 单文件；C++ 请使用原审查入口。")
        # Reuse existing limits even when no initial source has been supplied.
        normalize_source(source if source is not None else "# source pending\n", filename)
        limits = {**DEFAULT_LIMITS, **(budget_limits or {})}
        if max_model_calls is not None:
            limits["max_model_calls"] = max_model_calls
        if active_timeout is not None:
            limits["max_active_seconds"] = active_timeout
        if set(limits) != set(DEFAULT_LIMITS):
            raise ValueError("预算字段仅支持 max_model_calls/max_tokens/max_active_seconds。")
        for key, value in limits.items():
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0 or value != value or value == float("inf"):
                raise ValueError(f"预算 {key} 必须是非负有限数。")
        run_id = uuid.uuid4().hex
        versions = [artifact(source, filename, 0, None, "upload")] if source is not None else []
        original = versions[0]["id"] if versions else None
        if acceptance_id is None:
            # Compatibility for existing callers; new UI always supplies an explicit contract or 'none'.
            acceptance_id = "orders-v1" if task.strip() == ORDER_TASK.strip() else "none"
        if not isinstance(acceptance_id, str):
            raise ValueError("验收合同 ID 必须是字符串。")
        spec = get_task_spec(acceptance_id) if acceptance_id != "none" else None
        validate_configuration(validation_profile, acceptance_id)
        data = {"run_id": run_id, "parent_run_id": parent_run_id, "schema_version": 1,
                "workflow_version": "1.0", "created_at": utc_now(), "updated_at": utc_now(),
                "task": task.strip(), "task_hash": source_hash(task.strip()), "filename": filename,
                "mode": mode, "input_mode": "upload" if source is not None else "generation",
                "validation_profile": validation_profile, "scenario": scenario,
                "workflow_config": plan.model_dump(), "inspection_cycles": 0,
                "extension_manifest": extension_manifest, "node_outputs": {}, "node_output_history": [], "node_runs": [],
                "acceptance_id": acceptance_id,
                "acceptance_spec": spec.model_contract() if spec else {},
                "acceptance_hash": get_acceptance_hash(acceptance_id),
                "status": "created", "phase": "created", "next_node": ("inspect" if plan.parallel_checks else "review") if source is not None else "coding",
                "fix_round": 0, "max_fix_rounds": plan.max_fix_rounds, "versions": versions,
                "original_version": original, "current_version": original,
                "required_interfaces": public_interfaces(versions[0]["code"]) if versions else {},
                "review": {}, "validation": {}, "review_history": [], "validation_history": [],
                "repair_records": [], "events": [], "unresolved": [], "termination_reason": "",
                "recoverable": False, "error": "", "budget_limits": limits,
                "metrics": {"model_calls": 0, "http_attempts": 0, "prompt_tokens": 0,
                            "completion_tokens": 0, "tool_calls": 0, "active_seconds": 0.0,
                            "scripted_actions": 0}}
        if plan.canvas:
            data.update(next_node="canvas", canvas_hash=plan.canvas.digest(), canvas_cursor={"outcomes": {}, "repair": False}, canvas_edges=[])
        event(data, "create", "created", "离线预设流程；不调用真实模型。" if mode == "demo" else "真实模型运行；凭证仅运行时注入。")
        self._graph().update_state(self._config(run_id), {"data": data}, as_node="dispatch")
        return run_id

    @staticmethod
    def _budget(data):
        metrics, limits = data["metrics"], data["budget_limits"]
        if metrics["active_seconds"] >= limits["max_active_seconds"]:
            raise agents.BudgetExceeded("活动时间预算耗尽。")
        if data["mode"] == "live" and metrics["model_calls"] >= limits["max_model_calls"]:
            raise agents.BudgetExceeded("模型调用预算耗尽。")
        if metrics["prompt_tokens"] + metrics["completion_tokens"] >= limits["max_tokens"]:
            raise agents.BudgetExceeded("token 预算耗尽。")

    def retry_repair(self, run_id: str, max_fix_rounds: int = 2, budget_limits: dict | None = None,
                     max_model_calls: int | None = None, active_timeout: float | None = None) -> str:
        """Create a new, explicitly budgeted repair attempt; never call a model here."""
        parent = self.get_snapshot(run_id)
        if parent["status"] not in TERMINAL:
            raise ValueError("请先结束或恢复当前运行；只有已结束的记录可以建立新修复尝试。")
        if not parent.get("versions"):
            raise ValueError("该记录没有完整源码，无法从现有版本重新修复。")
        item = current_artifact(parent)
        try:
            context = agents.repair_context(parent, item["code"])
        except agents.ContractError as exc:
            raise ValueError(str(exc)) from None
        if not context["repair_targets"]:
            raise ValueError("当前版本没有绑定且已确认的修复目标，请先重新审查或补充明确任务。")
        if isinstance(max_fix_rounds, bool) or not isinstance(max_fix_rounds, int) or not 1 <= max_fix_rounds <= 2:
            raise ValueError("重新修复需要 1～2 轮新的业务修复预算。")
        # create_run validates and records the new budget without touching the parent.
        new_id = self.create_run(parent["task"], source=item["code"], filename=parent["filename"],
                                 mode=parent["mode"], max_fix_rounds=max_fix_rounds,
                                 validation_profile=parent["validation_profile"], scenario=parent["scenario"],
                                 budget_limits=budget_limits, max_model_calls=max_model_calls,
                                 active_timeout=active_timeout, parent_run_id=run_id,
                                 acceptance_id=parent["acceptance_id"],
                                 workflow_config={**parent.get("workflow_config", {}), "max_fix_rounds": max_fix_rounds,
                                                  "enable_repair": True, "pause_after_review": False})
        fresh = self.get_snapshot(new_id)
        for key in ("versions", "original_version", "current_version", "required_interfaces",
                    "review", "validation", "review_history", "validation_history", "acceptance_id", "acceptance_hash", "acceptance_spec"):
            if key in parent:
                fresh[key] = copy.deepcopy(parent[key])
        for key in ("events", "repair_records", "repair_attempts"):
            fresh[key] = [{**copy.deepcopy(entry), "inherited": True,
                           "source_run_id": entry.get("source_run_id", run_id)} for entry in parent.get(key, [])]
        fresh.update(input_mode=parent["input_mode"], next_node="fix", retry_of=run_id,
                     history_source_run_id=run_id, inherited_metrics=copy.deepcopy(parent["metrics"]),
                     retry_base_code_hash=item["hash"], budget_policy="new_explicit_budget",
                     unresolved=[target.get("title", "") for target in context["repair_targets"]])
        inherited = parent.get("inherited_total_metrics", parent.get("inherited_metrics", {}))
        fresh["inherited_total_metrics"] = {key: parent["metrics"].get(key, 0) + inherited.get(key, 0)
                                             for key in fresh["metrics"]}
        if fresh.get("canvas_hash"):
            fresh.update(next_node="canvas", canvas_cursor={"outcomes": {}, "repair": True})
        event(fresh, "retry_repair", "created", "从父记录当前源码和同哈希审查目标重新修复；使用独立新预算，父记录保持不变。")
        self._graph().update_state(self._config(new_id), {"data": fresh}, as_node="dispatch")
        return new_id

    @staticmethod
    def _inspection_node(data):
        return "inspect" if data.get("workflow_config", {}).get("parallel_checks", False) else "review"

    def _notify(self, data, on_event, *, kind, node, active_nodes, transient=True):
        if on_event is None:
            return
        snapshot = self.get_snapshot(data["run_id"])
        snapshot.update(execution_event={"kind": kind, "node": node}, active_node=node,
                        active_nodes=list(active_nodes), active=bool(active_nodes),
                        execution_stage="started" if kind == "node_start" else ("branch_finished" if transient else "finished"),
                        transient=transient)
        snapshot["node_runs"] = copy.deepcopy(data.get("node_runs", []))
        if kind == "node_start":
            snapshot["active_started_at"] = utc_now()
        on_event(snapshot)

    def _node(self, name: str, settings, on_event=None):
        def execute(state):
            data = copy.deepcopy(state["data"])
            data.update(status="running", phase=name, recoverable=False, error="")
            started = time.monotonic()
            started_at = utc_now()
            incoming = handoff(data, name)
            before_metrics = copy.deepcopy(data["metrics"])
            try:
                self._budget(data)
                if name != "inspect":
                    self._notify(data, on_event, kind="node_start", node=name, active_nodes=[name])
                if name not in {"validate", "gate", "inspect"} and data["mode"] == "live" and self.model_factory is None:
                    if settings is None:
                        raise ModelError("真实模型运行需要运行时 Settings；凭证不会保存到检查点。")
                    settings.validate()
                if name == "inspect":
                    self._perform_inspect(data, settings, started, on_event)
                elif name not in CORE_NODES:
                    self.node_registry.execute(name, data, settings, self.model_factory, started)
                    selected = data["workflow_config"]["extension_nodes"]
                    index = selected.index(name) + 1
                    data["next_node"] = selected[index] if index < len(selected) else "gate"
                else:
                    self._perform(name, data, settings, started)
                event(data, name, data["status"], self._message(name, data))
            except agents.BudgetExceeded as exc:
                data.update(status="needs_attention", termination_reason="budget_exhausted", error=str(exc))
                data["unresolved"].append(str(exc))
                event(data, name, "needs_attention", str(exc))
            except agents.ContractError as exc:
                data.update(status="needs_attention", termination_reason="invalid_output", error=str(exc))
                data["unresolved"].append(str(exc))
                event(data, name, "needs_attention", str(exc))
            except (ModelError, ValueError) as exc:
                message = str(exc)
                key = getattr(settings, "api_key", "") if settings is not None else ""
                if key:
                    message = message.replace(key, "[凭证已隐藏]")
                data.update(status="blocked", recoverable=True, next_node=name, error=message[:1500],
                            termination_reason="runtime_configuration")
                event(data, name, "blocked", message[:1500])
            except Exception as exc:
                # Avoid persisting arbitrary exception text that may contain transport secrets.
                message = f"节点 {name} 执行错误（{type(exc).__name__}）；已保留版本，可修复环境后继续。"
                data.update(status="blocked", recoverable=True, next_node=name, error=message,
                            termination_reason="runtime_error")
                event(data, name, "blocked", message)
            finally:
                data["metrics"]["active_seconds"] = round(data["metrics"]["active_seconds"] + time.monotonic() - started, 6)
                data["updated_at"] = utc_now()
            if data["metrics"]["active_seconds"] > data["budget_limits"]["max_active_seconds"]:
                data.update(status="needs_attention", recoverable=False, termination_reason="budget_exhausted",
                            error="活动时间预算耗尽，保留已有产物。")
            data.setdefault("node_runs", []).append({"id": uuid.uuid4().hex, "node": name,
                "started_at": started_at, "finished_at": utc_now(), "duration_seconds": round(time.monotonic() - started, 6),
                "round": incoming["fix_round"], "status": data["status"], "error": data.get("error", ""),
                "input": incoming, "output": handoff(data, name),
                "usage": {k: data["metrics"][k] - before_metrics.get(k, 0) for k in ("model_calls", "prompt_tokens", "completion_tokens")}})
            return {"data": data}
        return execute

    @staticmethod
    def _message(name, data):
        messages = {"coding": "编码产物已提交，进入独立审查。", "review": "当前版本审查已提交，进入确定性验证。",
                    "validate": "验证记录已提交；跳过不算通过。", "fix": "完整修复源码已提交，重新审查并验证。",
                    "inspect": "并行审查与验证已汇合并提交，进入统一判定。",
                    "gate": data.get("termination_reason") or "检测到问题，进入独立修复。"}
        return messages.get(name, "扩展 Agent 的结构化交接已保存，继续执行计划。")

    @staticmethod
    def _after_inspection(data):
        nodes = data.get("workflow_config", {}).get("extension_nodes", [])
        return nodes[0] if nodes else "gate"

    def _perform(self, name: str, data: dict, settings, started: float):
        if name == "coding":
            code, summary = agents.coding(data, settings, self.model_factory, started)
            item = artifact(code, data["filename"], 0, None, "coding")
            data["versions"] = [item]
            data.update(current_version=item["id"], original_version=item["id"], next_node=self._inspection_node(data))
            data["required_interfaces"] = public_interfaces(item["code"])
            data["coding_summary"] = summary
        elif name == "review":
            item = current_artifact(data)
            outcome = agents.review(data, item["code"], settings, self.model_factory, started)
            data["review"] = outcome
            data["review_history"].append(outcome)
            data["next_node"] = "validate"
            data["inspection_cycles"] = data.get("inspection_cycles", 0) + 1
        elif name == "validate":
            item = current_artifact(data)
            expected_contract = get_acceptance_hash(data["acceptance_id"])
            if data["acceptance_hash"] != expected_contract:
                raise agents.ContractError("验收用例版本已变化，旧运行不使用新合同；请建立新运行。")
            remaining = data["budget_limits"]["max_active_seconds"] - data["metrics"]["active_seconds"]
            outcome = self.validation_runner(item["code"], item["hash"], data["validation_profile"],
                                             data["acceptance_id"], timeout=max(0.1, min(10, remaining)))
            current_interfaces = public_interfaces(item["code"])
            changed = [name for name, signature in data.get("required_interfaces", {}).items()
                       if current_interfaces.get(name) != signature]
            if changed:
                outcome["checks"].append({"id": "preserved_interfaces", "name": "公开接口保留",
                                          "status": "failed", "detail": "公开接口被删除或改变：" + ", ".join(changed)})
                outcome["failed_count"] += 1
                if outcome["status"] != "error":
                    outcome["status"] = "failed"
            data["validation"] = outcome
            data["validation_history"].append(outcome)
            if outcome["status"] == "error":
                errors = [item["detail"] for item in outcome["checks"] if item["status"] == "error"]
                data.update(status="blocked", recoverable=True, next_node="validate", error="；".join(errors),
                            termination_reason="validation_environment")
            else:
                data["next_node"] = self._after_inspection(data)
        elif name == "gate":
            self._gate(data)
        elif name == "fix":
            item = current_artifact(data)
            result = agents.fix(data, item["code"], settings, self.model_factory, started)
            if result.base_code_hash != item["hash"]:
                raise agents.ContractError("修复结果引用了旧版本哈希，当前代码未更新。")
            code = normalize_source(result.revised_code, data["filename"])
            data["fix_round"] += 1
            record = result.model_dump()
            record["round"] = data["fix_round"]
            record["run_id"] = data["run_id"]
            data["repair_records"].append(record)
            if not meaningful_source_change(item["code"], code):
                data.update(status="needs_attention", termination_reason="no_progress")
                reasons = result.unresolved_items or ([result.change_summary] if result.change_summary else [])
                data["unresolved"] = list(dict.fromkeys(data["unresolved"] + reasons + ["修复未产生实质源码变化，保留原版本。 "]))
                return
            revised = artifact(code, data["filename"], len(data["versions"]), item["id"], "fix")
            if any(old["hash"] == revised["hash"] for old in data["versions"]):
                data.update(status="needs_attention", termination_reason="repeated_code")
                data["unresolved"].append("候选重复历史代码，停止循环。")
                return
            data["versions"].append(revised)
            data.update(current_version=revised["id"], review={}, validation={}, next_node=self._inspection_node(data))

    def _perform_inspect(self, data: dict, settings, started: float, on_event=None):
        """Join two isolated branch states; only this thread writes graph state."""
        digest = current_artifact(data)["hash"]
        branch_states = {name: copy.deepcopy(data) for name in ("review", "validate")}
        cached_review = data.get("review", {}).get("code_hash") == digest and data.get("review", {}).get("complete")
        cached_validation = (data.get("validation", {}).get("code_hash") == digest
                             and data.get("validation", {}).get("status") in {"passed", "failed"})
        needed = [name for name, cached in (("review", cached_review), ("validate", cached_validation)) if not cached]
        timings, failures = {}, {}

        def run_branch(name):
            branch = branch_states[name]
            begin = time.monotonic()
            timings[name] = {"node": name, "code_hash": digest, "started_at": utc_now(), "start_clock": begin}
            try:
                if name == "review" and data["mode"] == "live" and self.model_factory is None:
                    if settings is None:
                        raise ModelError("真实模型审查需要运行时 Settings；凭证不保存到检查点。")
                    settings.validate()
                self._perform(name, branch, settings, started)
            finally:
                timings[name].update(finished_at=utc_now(), end_clock=time.monotonic())

        if needed:
            with ThreadPoolExecutor(max_workers=2, thread_name_prefix="codelens-check") as pool:
                futures = {pool.submit(run_branch, name): name for name in needed}
                active = set(needed)
                self._notify(data, on_event, kind="node_start", node="inspect", active_nodes=needed)
                pending = set(futures)
                while pending:
                    done, pending = wait(pending, return_when=FIRST_COMPLETED)
                    for future in done:
                        name = futures[future]
                        try:
                            future.result()
                        except Exception as exc:
                            failures[name] = exc
                        branch = branch_states[name]
                        timing = timings[name]
                        branch_error = branch.get("error", "")
                        if name in failures:
                            branch_error = f"{type(failures[name]).__name__}：分支执行失败，详见汇合节点错误。"
                        data.setdefault("node_runs", []).append({"id": uuid.uuid4().hex, "node": name,
                            "started_at": timing["started_at"], "finished_at": timing["finished_at"],
                            "duration_seconds": round(timing["end_clock"] - timing["start_clock"], 6),
                            "round": data["fix_round"], "status": "blocked" if branch_error else "finished",
                            "error": branch_error, "input": handoff(data, name), "output": handoff(branch, name),
                            "usage": {k: branch["metrics"][k] - data["metrics"][k] for k in ("model_calls", "prompt_tokens", "completion_tokens")},
                            "parallel": True})
                        active.discard(name)
                        self._notify(data, on_event, kind="node_end", node=name, active_nodes=sorted(active))

        # Merge additive usage exactly once; activity time is measured as join wall time.
        original_metrics = copy.deepcopy(data["metrics"])
        for key, baseline in original_metrics.items():
            if key != "active_seconds":
                data["metrics"][key] = baseline + sum(
                    branch_states[name]["metrics"].get(key, baseline) - baseline for name in needed)
        for name, key, history_key in (("review", "review", "review_history"),
                                       ("validate", "validation", "validation_history")):
            if name in needed:
                branch = branch_states[name]
                data[key] = branch[key]
                data[history_key].extend(branch[history_key][len(data[history_key]):])
        data.setdefault("branch_timings", []).extend(timings.values())
        data["inspection_cycles"] = data.get("inspection_cycles", 0) + 1
        if failures:
            # Preserve completed sibling evidence and usage before the common error handler runs.
            for error in failures.values():
                if isinstance(error, agents.BudgetExceeded):
                    raise error
            raise next(iter(failures.values()))
        validation = data.get("validation", {})
        if validation.get("status") == "error":
            messages = [item.get("detail", "") for item in validation.get("checks", []) if item.get("status") == "error"]
            data.update(status="blocked", recoverable=True, next_node="inspect", error="；".join(messages),
                        termination_reason="validation_environment")
        else:
            data["next_node"] = self._after_inspection(data)

    @staticmethod
    def _gate(data):
        item = current_artifact(data)
        review, validation = data["review"], data["validation"]
        if review.get("code_hash") != item["hash"] or validation.get("code_hash") != item["hash"]:
            data.update(status="needs_attention", termination_reason="stale_evidence")
            data["unresolved"] = ["审查或验证不对应当前源码，拒绝判定通过。"]
            return
        blockers = [entry["title"] for entry in review.get("findings", []) if entry["severity"] in {"high", "medium"}]
        blockers.extend(entry["detail"] for entry in validation.get("checks", []) if entry["status"] == "failed")
        if review.get("requirements_status") == "gap":
            blockers.append(review.get("requirements_reason", "存在需求缺口。"))
        uncertain = [entry["title"] for entry in review.get("pending_findings", []) if entry["severity"] in {"high", "medium"}]
        if not review.get("complete"):
            uncertain.append("审查未完整完成或模型复核失败。")
        if review.get("requirements_status") == "unknown":
            uncertain.append(review.get("requirements_reason", "需求核对存在未知项。"))
        data["unresolved"] = list(dict.fromkeys(blockers + uncertain))
        if blockers:
            if not data.get("workflow_config", {}).get("enable_repair", True):
                data.update(status="needs_attention", termination_reason="repair_disabled")
            elif data["fix_round"] >= data["max_fix_rounds"]:
                data.update(status="needs_attention", termination_reason="fix_budget_exhausted")
            else:
                data["next_node"] = "fix"
        elif uncertain:
            data.update(status="needs_attention", termination_reason="unresolved_review")
        elif data["validation_profile"] in {"functional_required", "functional_portable"}:
            if not validation.get("functional_executed") or validation.get("failed_count") or validation.get("status") != "passed":
                data.update(status="needs_attention", termination_reason="functional_not_verified")
            else:
                data.update(status="completed_verified", termination_reason="frozen_acceptance_passed")
        else:
            data.update(status="completed_static", termination_reason="static_only_functionality_unverified")

    def get_snapshot(self, run_id: str) -> dict:
        snapshot = self._graph().get_state(self._config(run_id))
        if not snapshot.values or "data" not in snapshot.values:
            raise ValueError("未找到运行记录。")
        data = copy.deepcopy(snapshot.values["data"])
        if snapshot.next and data["status"] == "running" and (self.db_path, run_id) not in _EXECUTING:
            data["status"] = "paused"
        data["pending_nodes"] = list(snapshot.next)
        return data

    def list_runs(self) -> list[dict]:
        ids = [row[0] for row in self.connection.execute("SELECT DISTINCT thread_id FROM checkpoints WHERE checkpoint_ns = ''")]
        runs = [self.get_snapshot(run_id) for run_id in ids]
        return sorted(runs, key=lambda item: item["created_at"], reverse=True)

    def execute(self, run_id: str, settings=None, on_event=None, stop_after: str | None = None) -> dict:
        if stop_after not in {None, *CORE_NODES, *(spec.id for spec in self.node_registry.all())}:
            raise ValueError("未知暂停节点。")
        with _LOCKS_GUARD:
            lock = _LOCKS.setdefault((self.db_path, run_id), threading.Lock())
        if not lock.acquire(blocking=False):
            raise ValueError("该运行正在执行，请勿重复启动。")
        try:
            graph = self._graph(settings, on_event)
            config = self._config(run_id)
            previous = self.get_snapshot(run_id)
            if previous.get("workflow_version") != "1.0" or previous.get("schema_version") != 1:
                raise ValueError("运行状态版本与当前工作流不兼容，请保留旧记录并新建运行。")
            if previous["status"] in TERMINAL:
                return previous
            try:
                validate_configuration(previous["validation_profile"], previous["acceptance_id"])
                checked_plan = WorkflowConfig.model_validate(previous.get("workflow_config", {}))
                if checked_plan.canvas:
                    checked_plan.canvas.validate_for(self.node_registry)
                    if checked_plan.canvas.digest() != previous.get("canvas_hash"):
                        raise ValueError("已冻结的画布发生变化，请建立新运行。")
                manifest = self.node_registry.manifest(previous.get("workflow_config", {}).get("extension_nodes", []))
                if manifest != previous.get("extension_manifest", {}):
                    raise ValueError("扩展节点版本已变化，请恢复原版本或新建任务。")
            except ValueError as exc:
                previous.update(status="needs_attention", recoverable=False,
                                termination_reason="invalid_configuration", error=str(exc))
                event(previous, "preflight", "needs_attention", str(exc))
                graph.update_state(config, {"data": previous}, as_node="dispatch")
                return self.get_snapshot(run_id)
            plan = previous.get("workflow_config", {})
            canvas_mode = bool(plan.get("canvas"))
            if stop_after is None and plan.get("pause_after_review") and not previous.get("inspection_cycles", 0):
                stop_after = "review"
            if not canvas_mode and plan.get("parallel_checks") and stop_after in {"review", "validate"}:
                stop_after = "inspect"  # Branches finish together before a durable pause.
            raw = graph.get_state(config)
            has_interrupt = any(getattr(task, "interrupts", ()) for task in raw.tasks)
            command = Command(resume=True) if has_interrupt else None
            previous_event_id = previous["events"][-1]["id"] if previous["events"] else None
            _EXECUTING.add((self.db_path, run_id))
            seen_records = len(previous.get("node_runs", []))
            for value in graph.stream(command, config, stream_mode="values", durability="sync",
                                      interrupt_after=[stop_after] if stop_after and not canvas_mode else None):
                if on_event is not None and isinstance(value, dict) and "data" in value:
                    committed = self.get_snapshot(run_id)
                    latest = committed["events"][-1] if committed["events"] else None
                    if latest and latest["id"] != previous_event_id:
                        self._notify(committed, on_event, kind="node_end", node=latest["node"], active_nodes=[], transient=False)
                        previous_event_id = latest["id"]
                if canvas_mode and stop_after and isinstance(value, dict) and "data" in value:
                    new_records = value["data"].get("node_runs", [])[seen_records:]
                    if any(r["node"] == stop_after and not r.get("error") for r in new_records):
                        break
            _EXECUTING.discard((self.db_path, run_id))
            return self.get_snapshot(run_id)
        finally:
            _EXECUTING.discard((self.db_path, run_id))
            lock.release()

    def resume(self, run_id: str, settings=None, on_event=None, stop_after=None) -> dict:
        return self.execute(run_id, settings=settings, on_event=on_event, stop_after=stop_after)

    def close(self):
        self.connection.close()

    def export_json(self, run_id: str) -> str:
        return json.dumps(self.get_snapshot(run_id), ensure_ascii=False, indent=2)
