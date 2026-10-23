"""Local component assets; the server validates every client-supplied graph."""

import json
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from workflow.canvas import CanvasGraph, template_graph

_component = components.declare_component("codelens_canvas", path=str(Path(__file__).parent / "canvas_component"))


def render_canvas(plan, registry):
    saved = st.session_state.get("flow_canvas_graph") or plan.get("canvas") or template_graph(plan["parallel_checks"], plan["extension_nodes"])
    st.session_state.setdefault("flow_canvas_graph", saved)
    st.caption("拖动节点、连接分支并应用画布。普通连线为 always，条件出口为 true / false；修复出口必须返回检查入口。")
    left, right = st.columns(2)
    if left.button("从当前顺序 / 并行设置重建画布", key="canvas_rebuild"):
        saved = template_graph(plan["parallel_checks"], plan["extension_nodes"])
        st.session_state.flow_canvas_graph = saved
        st.session_state.canvas_generation = st.session_state.get("canvas_generation", 0) + 1
    upload = right.file_uploader("导入画布 JSON", type=["json"], key="canvas_import")
    if upload and right.button("应用导入文件", key="canvas_import_apply"):
        try:
            if upload.size > 100_000:
                raise ValueError("画布文件超过 100 KB。")
            saved = CanvasGraph.model_validate_json(upload.getvalue()).validate_for(registry).model_dump()
            st.session_state.flow_canvas_graph = saved
            st.session_state.canvas_generation = st.session_state.get("canvas_generation", 0) + 1
        except ValueError as exc:
            st.error(str(exc))
    result = _component(graph=saved, catalog=[{"id": s.id, "label": s.label} for s in registry.all()],
                        key=f"canvas_{st.session_state.get('canvas_generation', 0)}", default=None)
    if result and isinstance(result, dict) and isinstance(result.get("graph"), dict):
        saved = result["graph"]
        st.session_state.flow_canvas_graph = saved
    plan["canvas"] = saved
    try:
        graph = CanvasGraph.model_validate(saved).validate_for(registry)
        plan["extension_nodes"] = graph.extensions()
        st.success(f"画布校验通过 · {len(graph.nodes)} 个节点 · {len(graph.edges)} 条连线 · 指纹 {graph.digest()[:12]}")
    except ValueError as exc:
        st.error(f"画布暂不能执行：{exc}")
    st.download_button("导出画布 JSON", json.dumps(saved, ensure_ascii=False, indent=2), "codelens-workflow.json", "application/json", key="canvas_export")
    st.caption("开始运行会再次校验并冻结画布。连线控制实际调度；审查与验收必须执行，修复循环受轮数和预算约束。")
    return plan
