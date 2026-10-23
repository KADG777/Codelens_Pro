"""Repeated comparisons keep task clusters and negative evidence visible."""

import hashlib
import json

import streamlit as st

from compare_agent_value import ARMS, summarize
from compare_workflows import audit_status

LABELS = {"direct": "单次直接处理", "single_feedback": "单 Agent + 测试修复", "workflow": "多 Agent Workflow"}
OUTCOME = {"passed": "通过", "failed": "失败", "unavailable": "未验证"}


def load_report(path):
    raw = path.read_bytes()
    report = json.loads(raw)
    if report.get("kind") != "agent_value_comparison" or report.get("real_api_used") is not True:
        raise ValueError("不是有效的真实模型对照报告。")
    entries = report["entries"]
    ids = [e["id"] for e in entries]
    if ids != report["planned_case_ids"] or len(set(ids)) != len(ids):
        raise ValueError("计划条目缺失或重复。")
    if report.get("repetitions") != 3 or len({e["case_id"] for e in entries}) != 4 or len(entries) != 12:
        raise ValueError("本报告应包含 4 个任务、每项 3 次重复。")
    for case_id in {e["case_id"] for e in entries}:
        if sorted(e["repeat"] for e in entries if e["case_id"] == case_id) != [1, 2, 3]:
            raise ValueError("重复试验不完整。")
    return raw, report, summarize(entries)


def render_agent_value(root, filename="agent-value-live.json"):
    st.subheader("三组重复实验：分工与测试反馈各自带来了什么")
    path = root / "evals/results" / filename
    if not path.exists():
        st.info("尚无三组真实复测记录。")
        return
    try:
        raw, report, summary = load_report(path)
    except (ValueError, KeyError, TypeError) as exc:
        st.error(str(exc))
        return
    scope = "同任务工程复测" if report.get("engineering_retest") else "4 个新合成任务"
    st.caption(f"{report['model']} · {scope} × 3 次重复 × 3 组 · 报告指纹 {hashlib.sha256(raw).hexdigest()[:12]}")
    if report.get("engineering_retest"):
        st.info("这是修正协议处理后的同任务完整复测，不是新的盲测。上一次 8/12 的 Workflow 结果仍保留在“优化后新任务实验”中；两次共享同一 API 费用上限。")
    if report.get("diagnostics"):
        st.info("本报告使用精简审查、反例重跑和候选回归保护。旧任务复测列在下方，与新任务通过率分别统计。")
    st.write("中间组由同一个 Agent 接收公开测试反馈，最多生成一次、修复两次。工作流组拆分审查与修复角色，使用相同公开合同和修复轮数。隐藏测试在三组结果固定后评分，不反馈给任何 Agent。")
    feedback_trials = sum(len(e.get("single_feedback", {}).get("feedback_history", [])) > 1 for e in report["entries"])
    st.caption(f"本轮中间组实际进入第二次及以后生成：{feedback_trials} / 12 次。未触发反馈修复时，组间差异不能归因于测试反馈。")
    if not report.get("completed_at"):
        st.warning("实验进行中。未执行条目保留在分母中，此处不是最终结论。")
    st.dataframe([{"处理方式": LABELS[a], "通过 / 计划": f"{v['passed']} / {v['planned']}", "失败": v["failed"],
                   "未验证": v["unavailable"], "回归用例": v["regressed_checks"], "耗时（秒）": v["seconds"],
                   "token": v["tokens"], "调用次数": v["calls"], "每次成功的总 token 成本": v["tokens_per_success"]}
                  for a,v in summary["arms"].items()], hide_index=True, use_container_width=True)
    for baseline, values in summary["paired"].items():
        st.write(f"对照 {LABELS[baseline]}：只有 Workflow 通过 {values['workflow_only_pass']} 次；只有对照组通过 {values['baseline_only_pass']} 次。")
    st.dataframe([{"任务": case, **{LABELS[a]: " / ".join(OUTCOME[s] for s in values[a]) for a in ARMS}}
                  for case,values in summary["tasks"].items()], hide_index=True, use_container_width=True)
    st.warning("这仍然只有 4 个内部编写任务；3 次重复用于观察稳定性，不能当作 12 个独立任务。未证明普遍准确率优势。Workflow 的源码交接记录、独立验收和恢复能力需要与准确率、成本分开评价。")
    trace = summary["trace"]
    completed = sum(e.get("workflow", {}).get("status") == "completed_verified" for e in report["entries"])
    st.caption(f"Workflow 正常完成公开验收：{completed} / 12。上表按最终源码独立评分，流程中断时已有源码仍可能通过评分，两项分别记录。")
    st.caption(f"包含源码哈希交接记录：{trace['workflow_trials_with_bound_handoffs']} / 12 次 Workflow；实际进入修复：{trace['workflow_repair_trials']} 次。没有执行的测试不算通过。")
    chosen = st.selectbox("查看任务重复实验", [e["id"] for e in report["entries"]], key="value_trial")
    entry = next(e for e in report["entries"] if e["id"] == chosen)
    st.write(entry["task"])
    for arm in ARMS:
        value = entry.get(arm, {})
        with st.expander(f"{LABELS[arm]} · {OUTCOME[audit_status(value)]} · {value.get('status', '未执行')}"):
            st.json(value, expanded=False)
    st.download_button("下载三组实验原始报告", raw, "codelens-agent-value.json", "application/json", key="value_download")
    if report.get("engineering_retest"):
        st.caption(f"两轮合计（含旧任务检查）：{report['budget_usage']['calls']} 次调用，{report['budget_usage']['actual_tokens']} token；保守扣减 {report['budget_usage']['charged_tokens']} / 200000。")
        targeted_path = root / "evals/results/oracle-guard-check.json"
        if targeted_path.exists():
            targeted = json.loads(targeted_path.read_text(encoding="utf-8"))
            if targeted.get("primary_sha256") == hashlib.sha256(raw).hexdigest():
                with st.expander("最终修正：错误的模型预期不能否决正确候选", expanded=True):
                    st.write("原候选按新规则零 API 回放通过；一次真实定点复测也通过。这是事后验证，不改写上表 11/12，也不代表整套新盲测。")
                    st.caption(f"含定点验证的累计用量：{targeted['combined_usage']['calls']} 次调用，{targeted['combined_usage']['actual_tokens']} token；保守扣减 {targeted['combined_usage']['charged_tokens']} / 200000。")
                    st.json(targeted, expanded=False)
        return
    if report.get("diagnostics"):
        with st.expander("旧任务回归复测（每项一次，不计入上表）"):
            st.dataframe([{"任务": e["id"], "独立评分": OUTCOME[audit_status(e["workflow"])],
                "状态": e["workflow"].get("status", "未执行"),
                "调用": e["workflow"].get("metrics", {}).get("model_calls", 0),
                "token": sum(e["workflow"].get("metrics", {}).get(k, 0) for k in ("prompt_tokens", "completion_tokens"))}
                for e in report["diagnostics"]], hide_index=True, use_container_width=True)
        st.caption(f"本轮合计用量（含旧任务）：{report['budget_usage']['calls']} 次调用，{report['budget_usage']['actual_tokens']} token。")
        return
    recovery = root / "evals/results/recovery-probes.json"
    if recovery.exists():
        with st.expander("恢复能力的独立证据（故障注入，无真实模型调用）"):
            st.caption("主动制造一次验证器故障，关闭并重开服务，再从检查点恢复。此证据验证恢复机制，不计入真实模型通过率。")
            st.json(json.loads(recovery.read_text(encoding="utf-8")), expanded=False)
    real_recovery = root / "evals/results/real-recovery.json"
    if real_recovery.exists():
        probe = json.loads(real_recovery.read_text(encoding="utf-8"))
        with st.expander("真实 API 超时后的恢复（单独报告，不改主实验成绩）"):
            if probe.get("completed_at"):
                st.write(f"{probe['case_id']}：{probe['before_status']} → {probe['after_status']}；恢复后独立评分：{OUTCOME[probe['independent_outcome']]}。")
                st.caption(f"从 {probe['resumed_node']} 继续，原交接记录保留：{probe['preserved_prior_records']}。额外 {probe['extra_usage']['calls']} 次调用、{probe['extra_usage']['actual_tokens']} token。")
            st.json(probe, expanded=False)
