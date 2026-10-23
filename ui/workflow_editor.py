"""A small visual editor whose controls produce the executed workflow plan."""

import streamlit as st

from ui.execution_graph import build_execution_svg
from workflow.node_registry import default_registry

DEFAULT_PLAN = {"parallel_checks": False, "max_fix_rounds": 2, "enable_repair": True, "pause_after_review": False, "extension_nodes": []}
NODES = [
    ("coding", "① 编码 Agent", "根据本次目标生成完整源码。上传已有代码时自动跳过这个节点。"),
    ("review", "② 审查 Agent", "给出问题、源码证据与需求核对。可与独立验收并行执行，完成后汇合。"),
    ("validate", "③ 独立验收", "执行当前版本的静态或功能检查。验收结果独立于修复 Agent 的自述。"),
    ("fix", "④ 修复 Agent", "根据已汇合的审查与验证修复源码，再返回审查与验收；达到轮数或没有进展时结束。"),
]


def saved_editor_plan() -> dict:
    return {**DEFAULT_PLAN, **st.session_state.get("flow_workflow_config", {})}


def _select_node(node: str) -> None:
    st.session_state.flow_editor_node = node


def _reset_plan() -> None:
    st.session_state.flow_workflow_config = dict(DEFAULT_PLAN)
    st.session_state.flow_execution_order = "顺序执行"
    st.session_state.flow_enable_repair = True
    st.session_state.flow_max_rounds = 2
    st.session_state.flow_pause = False
    st.session_state.flow_extensions = []


def build_plan_svg(plan: dict, selected: str = "review") -> str:
    return build_execution_svg(plan, selected=selected)


def render_workflow_editor() -> dict:
    plan = saved_editor_plan()
    defaults = {"flow_execution_order": "并行执行" if plan["parallel_checks"] else "顺序执行",
                "flow_enable_repair": plan["enable_repair"], "flow_max_rounds": plan["max_fix_rounds"],
                "flow_pause": plan["pause_after_review"], "flow_extensions": plan.get("extension_nodes", [])}
    for key, value in defaults.items():
        st.session_state.setdefault(key, value)
    selected = st.session_state.get("flow_editor_node", "review")
    with st.expander("可视化流程编排", expanded=True):
        st.caption("点击节点查看配置与交接。下面的设置会保存到本次运行，并改变实际执行路径。")
        columns = st.columns(4)
        for column, (node, title, _) in zip(columns, NODES):
            column.button(title, type="primary" if selected == node else "secondary", on_click=_select_node,
                          args=(node,), key=f"flow_node_{node}", use_container_width=True)
        node = next(item for item in NODES if item[0] == selected)
        st.write(f"**{node[1]}** · {node[2]}")
        left, right = st.columns(2)
        with left:
            execution = st.radio("审查与独立验收", ["顺序执行", "并行执行"], horizontal=True, key="flow_execution_order",
                                 help="并行会真正同时启动两个独立分支，等待两份结果汇合后才判断和修复。")
            enable = st.checkbox("开启修复循环", key="flow_enable_repair")
        with right:
            rounds = st.number_input("最多修复轮数", min_value=0, max_value=2, step=1, key="flow_max_rounds", disabled=not enable)
            pause = st.checkbox("首次审查后暂停", key="flow_pause", help="顺序模式在审查提交后暂停；并行模式在审查和验收均完成并汇合后暂停。")
        registered = {spec.id: spec for spec in default_registry().all()}
        extensions = st.multiselect("汇合后接入扩展 Agent", list(registered), key="flow_extensions",
            format_func=lambda key: registered[key].label,
            help="按选择顺序执行，统一输入输出合同与运行预算；解读建议不会改变独立验收结论。")
        plan = {"parallel_checks": execution == "并行执行", "max_fix_rounds": int(rounds),
                "enable_repair": bool(enable), "pause_after_review": bool(pause), "extension_nodes": extensions}
        st.session_state.flow_workflow_config = plan
        st.markdown(build_plan_svg(plan, selected), unsafe_allow_html=True)
        path = "编码 → [ 审查 ∥ 独立验收 ] → 汇合判断" if plan["parallel_checks"] else "编码 → 审查 → 独立验收 → 判断"
        loop = f"发现问题 → 修复（最多 {rounds} 轮）→ 返回审查与验收" if enable and rounds else "发现问题 → 保留源码与问题清单，不执行修复"
        st.info(f"{path}\n\n{loop}" + ("\n\n已开启暂停；已提交节点不会在继续时重复执行。" if pause else ""))
        st.button("恢复默认流程", on_click=_reset_plan, key="flow_reset_plan")
    return plan
