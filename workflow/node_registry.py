"""Trusted, typed extension nodes; outputs cannot overwrite workflow authority."""

import copy
import re
from dataclasses import dataclass
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from workflow import agents
from workflow.models import current_artifact


class NodeInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    node_id: str
    code_hash: str
    task: str
    state: dict = Field(default_factory=dict)


class NodeOutput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    code_hash: str
    summary: str = Field(min_length=1, max_length=3000)


@dataclass(frozen=True)
class NodeContext:
    """Only a bounded model-call capability, not the mutable workflow state."""

    mode: str
    complete: Callable


@dataclass(frozen=True)
class NodeSpec:
    id: str
    version: str
    label: str
    description: str
    handler: Callable
    reads: tuple[str, ...] = ("review", "validation")
    input_schema: type[NodeInput] = NodeInput
    output_schema: type[NodeOutput] = NodeOutput


CORE_NODES = ("coding", "review", "validate", "inspect", "gate", "fix")
READABLE = {"review", "validation", "acceptance_spec", "fix_round", "node_outputs", "input_mode"}


class NodeRegistry:
    def __init__(self):
        self._nodes: dict[str, NodeSpec] = {}

    def register(self, spec: NodeSpec) -> None:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,47}", spec.id) or spec.id in {*CORE_NODES, "dispatch", "pause", "finish"}:
            raise ValueError("扩展节点 ID 非法或占用了系统节点。")
        if spec.id in self._nodes or not spec.version or not callable(spec.handler):
            raise ValueError("扩展节点需要唯一 ID、版本和处理函数。")
        if not set(spec.reads) <= READABLE:
            raise ValueError("扩展节点只能读取声明的公开交接字段。")
        if not issubclass(spec.input_schema, NodeInput) or not issubclass(spec.output_schema, NodeOutput):
            raise ValueError("节点合同必须继承 NodeInput / NodeOutput。")
        self._nodes[spec.id] = spec

    def all(self) -> tuple[NodeSpec, ...]:
        return tuple(self._nodes.values())

    def get(self, node_id: str) -> NodeSpec:
        if node_id not in self._nodes:
            raise ValueError(f"扩展节点未安装：{node_id}；请恢复相同插件版本或新建任务。")
        return self._nodes[node_id]

    def manifest(self, ids: list[str]) -> dict:
        if len(ids) != len(set(ids)):
            raise ValueError("扩展节点不能重复。")
        return {node_id: self.get(node_id).version for node_id in ids}

    def execute(self, node_id, data, settings, factory, started):
        spec = self.get(node_id)
        item = current_artifact(data)
        request = spec.input_schema.model_validate({"node_id": node_id, "code_hash": item["hash"],
                    "task": data["task"], "state": {key: copy.deepcopy(data.get(key)) for key in spec.reads}})

        def complete(schema, system, payload):
            model = agents.role_model(node_id, settings, factory, data, started)
            try:
                return agents.structured(model, schema, system + agents.TASK_AUTHORITY, payload, max_tokens=2048)
            finally:
                agents.close_model(model)

        try:
            result = spec.handler(request, NodeContext(data["mode"], complete))
            result = spec.output_schema.model_validate(result.model_dump() if isinstance(result, BaseModel) else result)
            if result.code_hash != item["hash"]:
                raise agents.ContractError("扩展节点输出绑定了错误的源码版本。")
        except ValidationError:
            raise agents.ContractError(f"扩展节点 {node_id} 输出不符合注册合同。") from None
        # The only allowed write destination: a namespaced, source-bound record.
        output = {"node_id": node_id, "node_version": spec.version, **result.model_dump()}
        data.setdefault("node_outputs", {})[node_id] = output
        data.setdefault("node_output_history", []).append(output)
        if data["mode"] == "demo":
            data["metrics"]["scripted_actions"] += 1


class ExplanationOutput(NodeOutput):
    risks: list[str] = Field(default_factory=list, max_length=8)
    next_steps: list[str] = Field(default_factory=list, max_length=8)


def explain_acceptance(request: NodeInput, context: NodeContext) -> ExplanationOutput:
    if context.mode == "demo":
        validation = request.state.get("validation") or {}
        failures = [c.get("detail", "") for c in validation.get("checks", []) if c.get("status") == "failed"]
        return ExplanationOutput(code_hash=request.code_hash, summary="预设解读：验收记录已整理；通过判定仍由独立验收决定。",
                                 risks=failures[:8], next_steps=["修复后按同一合同重新验收。"] if failures else [])
    return context.complete(ExplanationOutput,
        "你是验收解读 Agent。用中文解释审查与实际验收之间的差异，列出风险和下一步。"
        "只解释所提供的证据；不要生成源码、修改通过状态或声称未执行的性能提升。保留原 code_hash。",
        request.model_dump())


def default_registry() -> NodeRegistry:
    registry = NodeRegistry()
    registry.register(NodeSpec("acceptance_explainer", "1.0", "验收解读 Agent",
        "汇合审查和验收证据，解释仍有哪些风险；不修改源码或独立验收结论。",
        explain_acceptance, output_schema=ExplanationOutput))
    return registry
