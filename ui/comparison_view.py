"""Paired comparison dashboard derived from saved, source-bound test evidence."""

import hashlib
import json
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from compare_workflows import ARMS, audit_status, summarize_pairs
from ui.diff_view import build_side_by_side_html


def render_comparison(root: Path):
    if (root / "evals/results/agent-value-live.json").exists():
        from ui.agent_value_view import render_agent_value
        choice = st.radio("实验报告", ["三组重复实验", "首轮两组实验（保留）"], horizontal=True, key="comparison_report")
        if choice == "三组重复实验":
            render_agent_value(root)
            return
    path = root / "evals/results/paired-live.json"
    st.subheader("同任务：直接处理与完整工作流")
    if not path.is_file():
        st.info("尚未保存同任务对照实验。运行 compare_workflows.py 后，这里展示实际结果。")
        return
    try:
        raw = path.read_bytes()
        report = json.loads(raw)
        if report.get("kind") != "paired_comparison" or report.get("real_api_used") is not True:
            raise ValueError("该报告不是已标记的真实配对实验。")
        entries = report["entries"]
        if [e["id"] for e in entries] != report["planned_case_ids"]:
            raise ValueError("报告缺少计划中的任务，不能计算完整成功率。")
        totals = summarize_pairs(entries)
    except (ValueError, KeyError, TypeError) as exc:
        st.warning(f"无法读取完整对照证据：{exc}")
        return
    st.caption(f"同一模型 {report.get('model')} · 温度 {report.get('temperature')} · 每组运行 {report.get('repetitions')} 次 · 报告 {hashlib.sha256(raw).hexdigest()[:12]}")
    st.write("直接处理组只调用一次模型；工作流组运行审查、独立验收、必要时修复，并接入验收解读 Agent。两组使用同一任务与初始源码。")
    if not report.get("completed_at"):
        st.info("实验尚未全部结束；未执行项保留在分母中，不能作为最终结论。")
    rows = []
    labels = {"direct": "单模型直接处理", "workflow": "完整工作流"}
    for split, split_label in (("all", "全部"), ("development", "已有任务"), ("fresh", "新任务")):
        for arm in ARMS:
            value = totals[split][arm]
            rows.append({"分组": split_label, "方式": labels[arm], "独立验收通过": f"{value['passed']} / {value['planned']}",
                         "通过率": f"{value['pass_rate']:.1%}" if value["pass_rate"] is not None else "无任务",
                         "失败": value["failed"], "未验证": value["unavailable"], "回归任务": value["regressions"],
                         "回归用例": value["regressed_checks"], "耗时（秒）": value["seconds"], "token": value["tokens"], "模型调用": value["calls"]})
    st.dataframe(rows, hide_index=True, use_container_width=True)
    all_totals = totals["all"]
    st.write(f"仅工作流通过：{all_totals['workflow_only_passes']} 项；仅直接处理通过：{all_totals['direct_only_passes']} 项。")
    st.caption("成功要求公开用例和保留用例均实际执行且通过；回归指原本通过的任务或用例变为失败。耗时包含各组内部工作，排除最后的独立评分；工作流因此包含检查与多轮协作的成本。")
    st.warning("样本为 4 个已有任务和 4 个新合成任务，每组单次运行。新任务在实验前冻结，但由本项目编写，不是外部盲测；不据此宣称普遍优于单模型。")
    outcome_labels = {"passed": "通过", "failed": "失败", "unavailable": "未验证"}
    st.dataframe([{"任务": e["id"], "分组": "新任务" if e["split"] == "fresh" else "已有任务",
                   "直接处理": outcome_labels[audit_status(e.get("direct", {}))],
                   "工作流": outcome_labels[audit_status(e.get("workflow", {}))],
                   "执行顺序": " → ".join(labels[a] for a in e.get("execution_order", []))} for e in entries],
                 hide_index=True, use_container_width=True)
    chosen = st.selectbox("查看一对任务的原始证据", [e["id"] for e in entries], key="paired_case")
    entry = next(e for e in entries if e["id"] == chosen)
    st.write(entry["task"])
    direct, workflow = entry.get("direct", {}), entry.get("workflow", {})
    if direct.get("code") is not None and workflow.get("code") is not None:
        with st.expander("两组最终源码对比"):
            components.html(build_side_by_side_html(direct["code"], workflow["code"], before_label="直接处理结果", after_label="工作流结果"), height=450, scrolling=True)
    for arm in ARMS:
        with st.expander(labels[arm] + " · 独立用例与用量"):
            st.json(entry.get(arm, {}), expanded=False)
    st.caption(f"任务清单冻结时间：{report.get('frozen_at')} · 清单 SHA-256：{report.get('suite_sha256')}")
    st.download_button("下载同任务对照报告", data=raw, file_name="codelens-paired-comparison.json", mime="application/json", key="paired_download")
