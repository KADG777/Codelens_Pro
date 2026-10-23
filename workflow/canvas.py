"""Validated executable graph, with a bounded source-repair back edge."""

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class CanvasNode(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: str = Field(pattern=r"^[a-z][a-z0-9_]{0,47}$")
    kind: Literal["agent", "condition"] = "agent"
    predicate: Literal["always", "validation_failed", "review_findings"] = "always"
    x: float = Field(ge=0, le=1600, allow_inf_nan=False)
    y: float = Field(ge=0, le=1200, allow_inf_nan=False)


class CanvasEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source: str
    target: str
    when: Literal["always", "true", "false", "repair", "done"] = "always"


class CanvasGraph(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    version: Literal[1] = 1
    nodes: list[CanvasNode] = Field(min_length=6, max_length=24)
    edges: list[CanvasEdge] = Field(min_length=6, max_length=64)

    def digest(self):
        return hashlib.sha256(json.dumps(self.model_dump(), sort_keys=True, separators=(",", ":")).encode()).hexdigest()

    def extensions(self):
        return [n.id for n in self.nodes if n.kind == "agent" and n.id not in {"coding", "review", "validate", "gate", "fix", "finish"}]

    def forward_edges(self):
        return [e for e in self.edges if e.source not in {"gate", "fix"}]

    def validate_for(self, registry):
        nodes = {n.id: n for n in self.nodes}
        required = {"coding", "review", "validate", "gate", "fix", "finish"}
        if len(nodes) != len(self.nodes) or not required <= nodes.keys():
            raise ValueError("画布必须包含唯一的编码、审查、验收、判断、修复和结束节点。")
        if any(nodes[n].kind != "agent" for n in required):
            raise ValueError("系统节点类型不能改变。")
        registry.manifest(self.extensions())
        if any(n.kind == "condition" and not n.id.startswith("condition_") for n in self.nodes):
            raise ValueError("条件节点 ID 必须以 condition_ 开头。")
        edges = [(e.source, e.target, e.when) for e in self.edges]
        if len(edges) != len(set(edges)) or any(s not in nodes or t not in nodes or s == t for s, t, _ in edges):
            raise ValueError("连线存在重复、自连接或不存在的节点。")
        if {(e.target, e.when) for e in self.edges if e.source == "gate"} != {("fix", "repair"), ("finish", "done")}:
            raise ValueError("判断节点必须保留 repair → 修复、done → 结束两条出口。")
        entry = {e.target for e in self.edges if e.source == "coding"}
        if not entry or {(e.target, e.when) for e in self.edges if e.source == "fix"} != {(t, "always") for t in entry}:
            raise ValueError("修复必须返回编码之后的相同入口，重新审查当前源码。")
        incoming = {n: [] for n in nodes}
        outgoing = {n: [] for n in nodes}
        for e in self.forward_edges():
            if e.source == "finish" or e.target in {"coding", "fix", "finish"}:
                raise ValueError("不允许绕过判断节点结束，或建立额外的源码写入循环。")
            allowed = {"true", "false"} if nodes[e.source].kind == "condition" else {"always"}
            if e.when not in allowed:
                raise ValueError("条件出口须标记 true / false，普通出口须标记 always。")
            incoming[e.target].append(e.source)
            outgoing[e.source].append(e.target)
        for n in self.nodes:
            if n.kind == "condition" and {e.when for e in self.edges if e.source == n.id} != {"true", "false"}:
                raise ValueError("条件节点必须同时连接 true 和 false 出口。")
        body = set(nodes) - {"fix", "finish"}
        ancestors = {}
        remaining = set(body)
        while remaining:
            ready = sorted(n for n in remaining if set(incoming[n]) <= ancestors.keys())
            if not ready:
                raise ValueError("只允许受修复轮数约束的回路；检查阶段不能存在循环。")
            for n in ready:
                if n != "coding" and not incoming[n] or n != "gate" and not outgoing[n]:
                    raise ValueError("所有节点都必须从编码入口可达，并最终汇合至判断节点。")
                ancestors[n] = set(incoming[n]).union(*(ancestors[p] for p in incoming[n]))
                remaining.remove(n)
        conditions = {n.id for n in self.nodes if n.kind == "condition"}
        if any(ancestors[n] & conditions for n in ("review", "validate")):
            raise ValueError("审查和独立验收不能置于可跳过的条件分支中。")
        for n in self.nodes:
            reads = ()
            if n.id in self.extensions():
                reads = registry.get(n.id).reads
            elif n.kind == "condition":
                reads = {"validation_failed": ("validation",), "review_findings": ("review",)}.get(n.predicate, ())
            dependencies = {"review" if r == "review" else "validate" for r in reads if r in {"review", "validation"}}
            if not dependencies <= ancestors.get(n.id, set()):
                raise ValueError(f"节点 {n.id} 必须位于它读取的审查 / 验收节点之后。")
        return self


def template_graph(parallel=False, extensions=()):
    ids = ["coding", "review", "validate", *extensions, "gate", "fix", "finish"]
    nodes = [{"id": n, "x": float(35 + (i % 4) * 245), "y": float(60 + (i // 4) * 180)} for i, n in enumerate(ids)]
    pairs = [("coding", "review"), ("coding", "validate"), ("review", next(iter(extensions), "gate"))] if parallel else [("coding", "review"), ("review", "validate")]
    after = ["validate", *extensions, "gate"]
    pairs += list(zip(after, after[1:]))
    pairs += [("fix", "review")]
    if parallel:
        pairs.append(("fix", "validate"))
    edges = [{"source": s, "target": t, "when": "always"} for s, t in pairs]
    edges += [{"source": "gate", "target": t, "when": w} for t, w in (("fix", "repair"), ("finish", "done"))]
    return CanvasGraph.model_validate({"nodes": nodes, "edges": edges}).model_dump()


def predicate_value(node, data):
    if node.predicate == "validation_failed":
        return bool(data.get("validation", {}).get("failed_count"))
    if node.predicate == "review_findings":
        return bool(data.get("review", {}).get("findings"))
    return True


def ready_nodes(graph, outcomes):
    """Resolve skipped paths, then release every ready sibling (OR activation, join all)."""
    incoming = {n.id: [] for n in graph.nodes if n.id not in {"fix", "finish"}}
    for edge in graph.forward_edges():
        incoming[edge.target].append(edge)
    while True:
        ready, changed = [], False
        for node, edges in incoming.items():
            if node in outcomes or not all(e.source in outcomes for e in edges):
                continue
            active = node == "coding" or any(edge_taken(e, outcomes) for e in edges)
            if active:
                ready.append(node)
            else:
                outcomes[node] = "skipped"
                changed = True
        if not changed:
            return ready


def edge_taken(edge, outcomes):
    value = outcomes.get(edge.source)
    return value is not None and value != "skipped" and (edge.when == "always" or edge.when == value)
