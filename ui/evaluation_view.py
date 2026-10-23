"""Read saved synthetic evaluation evidence; this page never runs models or code."""

import hashlib
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import streamlit as st
import streamlit.components.v1 as components

from review_agent.config import ROOT
from ui.diff_view import build_side_by_side_html, diff_counts
from workflow.registry import list_task_specs

REPORT_SOURCES = {
    "public_live": ("随包真实评测", "evals/results/workflow-live.json"),
    "local_live": ("本机最新真实评测", "reports/workflow-live.json"),
    "public_offline": ("随包离线流程评测", "evals/results/workflow-smoke.json"),
    "local_offline": ("本机离线流程评测", "reports/workflow-smoke.json"),
}
AUDIT_LABELS = {"passed": "实际通过", "failed": "实际未通过", "unavailable": "未验证 / 不可用", "not_run": "未执行"}
ROLE_LABELS = {"coding": "编码", "review": "审查", "fix": "修复"}
FLOW_LABELS = {"completed_verified": "功能验收完成", "completed_static": "仅静态完成", "needs_attention": "仍需处理", "blocked": "环境阻塞", "failed": "运行中断"}


def load_saved_report(root: Path, source: str) -> tuple[dict, str]:
    if source not in REPORT_SOURCES:
        raise ValueError("请选择页面提供的固定报告来源。")
    path = root / REPORT_SOURCES[source][1]
    if not path.is_file():
        raise ValueError("尚未保存这份报告。可选择其他已有报告；此页面不会启动评测或调用 API。")
    if path.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("报告超过页面可读取的大小，请使用评测工具导出的精简报告。")
    try:
        payload = path.read_bytes()
        data = json.loads(payload.decode("utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("无法读取这份评测报告，请检查文件是否完整。") from exc
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list) or not all(isinstance(entry, dict) for entry in data["entries"]):
        raise ValueError("报告缺少逐任务的评测记录，不能据此统计成功。")
    return data, hashlib.sha256(payload).hexdigest()


def source_codes(entry: dict) -> tuple[str | None, str | None]:
    snapshot = entry.get("snapshot") or {}
    versions = {item.get("id"): item for item in snapshot.get("versions", [])}
    before = entry.get("initial_code")
    after = entry.get("final_code")
    if not isinstance(before, str):
        before = versions.get(snapshot.get("original_version"), {}).get("code")
    if not isinstance(after, str):
        after = versions.get(snapshot.get("current_version"), {}).get("code")
    return before, after


def audit_evidence(entry: dict, stage: str) -> dict:
    audit = entry.get(f"{stage}_audit") or {}
    checks = audit.get("checks") or []
    checks = [check for check in checks if isinstance(check, dict)]
    executed = audit.get("executed") is True
    code = source_codes(entry)[0 if stage == "initial" else 1]
    digest = hashlib.sha256(code.encode("utf-8")).hexdigest() if isinstance(code, str) else None
    bound = bool(digest) and audit.get("code_hash") == digest
    if not audit:
        status = "not_run"
    elif not executed or not bound or not checks:
        status = "unavailable"
    elif audit.get("status") == "failed" or any(check.get("status") == "failed" for check in checks):
        status = "failed"
    elif audit.get("status") == "passed" and all(check.get("status") == "passed" for check in checks):
        status = "passed"
    else:
        status = "unavailable"
    return {"status": status, "label": AUDIT_LABELS[status], "executed": executed, "source_bound": bound,
            "checks": checks, "passed": sum(c.get("status") == "passed" for c in checks),
            "failed": sum(c.get("status") == "failed" for c in checks), "raw": audit}


def evaluation_totals(report: dict) -> dict:
    entries = report.get("entries") or []
    eligible = [entry for entry in entries if entry.get("expectation") != "safe_stop" and entry.get("task_id") not in {None, "none"}]
    stages = {}
    for stage in ("initial", "final"):
        outcomes = [audit_evidence(entry, stage)["status"] for entry in eligible]
        stages[stage] = {status: outcomes.count(status) for status in AUDIT_LABELS}
    repairs = [entry for entry in eligible if audit_evidence(entry, "initial")["status"] == "failed"]
    safe = [entry for entry in entries if entry.get("expectation") == "safe_stop"]
    generated = [entry for entry in entries if entry.get("input_mode") == "generation"]
    three_roles = [entry["id"] for entry in generated if {"coding", "review", "fix"}.issubset(set(entry.get("roles_executed") or []))]
    summary = report.get("summary") or {}
    return {"planned": len(report.get("planned_case_ids") or entries), "recorded": len(entries), "eligible": len(eligible),
            "verified": sum(e.get("workflow_status") == "completed_verified" and audit_evidence(e, "final")["status"] == "passed" for e in entries),
            "safe_stops": sum(_safe_stop(e) for e in safe),
            "stages": stages, "repair_opportunities": len(repairs),
            "repair_successes": sum(audit_evidence(e, "final")["status"] == "passed" and source_codes(e)[0] != source_codes(e)[1] for e in repairs),
            "regressions": sum(audit_evidence(e, "initial")["status"] == "passed" and audit_evidence(e, "final")["status"] == "failed" for e in eligible),
            "changed_clean": sum(source_codes(e)[0] != source_codes(e)[1] for e in entries if e.get("expectation") == "clean_control"),
            "generated": len(generated), "initially_correct_generation": sum(audit_evidence(e, "initial")["status"] == "passed" for e in generated), "three_role_generation_cases": three_roles,
            "actual_model_calls": summary.get("actual_model_calls", 0),
            "actual_tokens": summary.get("actual_prompt_tokens", 0) + summary.get("actual_completion_tokens", 0),
            "scripted_model_calls": summary.get("scripted_model_calls", 0),
            "active_seconds": (summary.get("workflow_metrics") or {}).get("active_seconds", 0)}


def _safe_stop(entry: dict) -> bool:
    return (entry.get("expectation") == "safe_stop" and entry.get("workflow_status") == "needs_attention"
            and entry.get("termination_reason") in {"unresolved_review", "functional_not_verified"})


def _roles(entry: dict) -> list[str]:
    return [key for key in ROLE_LABELS if key in (entry.get("roles_executed") or [])]


def _stage_bars(totals: dict) -> str:
    rows = []
    denominator = max(1, totals["eligible"])
    for key, label in [("initial", "初稿"), ("final", "最终")]:
        counts = totals["stages"][key]
        segments = "".join(f'<span class="eval-{status}" style="width:{counts[status] / denominator * 100:.4f}%" title="{AUDIT_LABELS[status]} {counts[status]}"></span>' for status in AUDIT_LABELS)
        rows.append(f'<div class="eval-bar-row"><b>{label}</b><div class="eval-track">{segments}</div><span>通过 {counts["passed"]} / {totals["eligible"]}</span></div>')
    return """<style>.eval-bar-row{display:grid;grid-template-columns:50px minmax(100px,1fr) 110px;gap:15px;align-items:center;margin:15px 0;color:#17344d;font:14px system-ui,sans-serif}.eval-track{display:flex;height:22px;background:#edf1f5;border-radius:7px;overflow:hidden}.eval-passed{background:#13977f}.eval-failed{background:#dc6b68}.eval-unavailable,.eval-not_run{background:#b8c5cf}</style>""" + "".join(rows)


def _value(value) -> str:
    return json.dumps(value, ensure_ascii=False) if value is not None else "未记录返回值"


def _render_checks(entry: dict) -> None:
    for stage, label in [("initial", "初稿验收"), ("final", "最终验收")]:
        evidence = audit_evidence(entry, stage)
        with st.expander(f"{label} · {evidence['label']} · {evidence['passed']} 项通过 / {evidence['failed']} 项未通过", expanded=stage == "final"):
            if not evidence["executed"]:
                st.info("没有实际执行记录，不能计为功能通过。")
            elif not evidence["source_bound"]:
                st.warning("验收记录与这份源码的标识不一致，不能作为当前版本通过的证据。")
            if evidence["checks"]:
                st.dataframe([{"用例": check.get("name", check.get("id", "")), "结果": {"passed": "通过", "failed": "未通过", "error": "出错", "skipped": "未执行"}.get(check.get("status"), "未确认"),
                               "期望": _value(check.get("expected")), "实际": _value(check.get("actual"))} for check in evidence["checks"]], hide_index=True, use_container_width=True)
            raw = evidence["raw"]
            st.caption(f"源码标识：{raw.get('code_hash', '未记录')} · 执行器：{raw.get('backend', '未记录')} · {'独立保留用例' if raw.get('holdout') else '报告记录的用例'}")
            if raw.get("error"):
                st.write(raw["error"])


def render_evaluation_page(root: Path = ROOT) -> None:
    st.title("评测与验收")
    st.write("从保存的任务、源码和逐例验收结果，查看流程是否真正带来了改善。")
    source = st.selectbox("报告来源", list(REPORT_SOURCES), format_func=lambda key: REPORT_SOURCES[key][0] + ("" if (root / REPORT_SOURCES[key][1]).is_file() else "（尚未保存）"), key="flow_eval_source")
    try:
        report, digest = load_saved_report(root, source)
    except ValueError as error:
        st.info(str(error))
        return
    real = report.get("real_api_used") is True
    totals = evaluation_totals(report)
    mode_label = "真实模型评测 · 已实际调用 API" if real else "离线固定产物 · 不代表模型修复能力"
    st.info(mode_label)
    created = str(report.get("created_at", ""))
    try:
        created = datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S 北京时间")
    except ValueError:
        pass
    st.caption(f"来源：{REPORT_SOURCES[source][1]} · 生成时间：{created or '未记录'} · 报告标识 {digest[:12]}")
    columns = st.columns(4)
    columns[0].metric("功能验收完成", f"{totals['verified']} / {totals['planned']}")
    columns[1].metric("失败初稿 → 最终通过", f"{totals['repair_successes']} / {totals['repair_opportunities']}")
    columns[2].metric("回归", totals["regressions"], help="初稿独立验收通过、最终独立验收失败的任务数。")
    columns[3].metric("安全停机", totals["safe_stops"], help="效果目标尚未实测时保持待处理，单独展示，不计为目标达成。")
    st.warning("这是少量合成任务的结果，不是通用代码修复正确率。未执行、环境不可用和效果未测均不计成功；红绿变化也不等于验收通过。")
    st.subheader("初稿与最终的独立验收")
    st.caption(f"同 {totals['eligible']} 个计入功能修复统计的任务；效果未测的安全停机任务单独列示。绿色通过，红色未通过，灰色未验证或未执行。")
    st.markdown(_stage_bars(totals), unsafe_allow_html=True)
    stage_rows = [{"阶段": label, "实际通过": totals["stages"][key]["passed"], "实际未通过": totals["stages"][key]["failed"],
                   "未验证 / 不可用": totals["stages"][key]["unavailable"], "未执行": totals["stages"][key]["not_run"]} for key, label in [("initial", "初稿"), ("final", "最终")]]
    st.dataframe(stage_rows, hide_index=True, use_container_width=True)
    unverified = totals["stages"]["final"]["unavailable"] + totals["stages"]["final"]["not_run"]
    st.caption(f"最终未通过 {totals['stages']['final']['failed']} 项，未验证或未执行 {unverified} 项；效果未测的安全停机 {totals['safe_stops']} 项单独保留。")
    if totals["recorded"] < totals["planned"]:
        st.warning(f"还有 {totals['planned'] - totals['recorded']} 个计划任务没有逐任务记录，不能计为成功。")
    st.caption(f"正确代码对照被修改：{totals['changed_clean']} 项。生成入口共 {totals['generated']} 项，其中实际执行三个角色的生成任务 {len(totals['three_role_generation_cases'])} 项。")
    if real and totals["generated"] and not totals["three_role_generation_cases"]:
        if totals["initially_correct_generation"] == totals["generated"]:
            st.info("本次真实生成任务首稿已正确，没有触发修复 Agent；不能把这些记录称为三个角色全部执行的真实样例。")
        else:
            st.info("本报告没有三个角色全部执行的真实生成样例；各任务未触发修复的原因须结合实际记录判断。")
    labels = {spec["id"]: spec["label"] for spec in list_task_specs()}
    entries = report["entries"]
    st.subheader("逐任务结果")
    rows = []
    for entry in entries:
        before, after = source_codes(entry)
        rows.append({"任务": labels.get(entry.get("task_id"), entry.get("task_id", entry.get("id", ""))), "样例编号": entry.get("id", ""),
                     "入口": "需求生成" if entry.get("input_mode") == "generation" else "已有源码",
                     "初稿": audit_evidence(entry, "initial")["label"], "最终": audit_evidence(entry, "final")["label"],
                     "流程结论": "安全停机 · 效果未测" if _safe_stop(entry) else FLOW_LABELS.get(entry.get("workflow_status"), entry.get("case_status", "未执行")),
                     "源码变化": "有修改" if before is not None and after is not None and before != after else "未修改 / 未产生",
                     "实际角色": "、".join(ROLE_LABELS[key] for key in _roles(entry)),
                     "API 调用": (entry.get("metrics") or {}).get("model_calls", 0) if real else 0})
    st.dataframe(rows, hide_index=True, use_container_width=True)
    if entries:
        by_id = {entry.get("id", str(index)): entry for index, entry in enumerate(entries)}
        chosen = st.selectbox("查看任务证据", list(by_id), format_func=lambda key: labels.get(by_id[key].get("task_id"), key) + " · " + key, key="flow_eval_case")
        entry = by_id[chosen]
        with st.container(border=True):
            st.markdown("**任务目标**")
            st.write(entry.get("task") or "报告未保存任务文字。")
            st.caption(f"运行编号：{entry.get('run_id', '未执行')} · 实际角色：{'、'.join(ROLE_LABELS[key] for key in _roles(entry)) or '未记录'}")
            before, after = source_codes(entry)
            if before is not None and after is not None:
                added, removed = diff_counts(before, after)
                st.caption(f"真实保存源码：新增 {added} 行 / 删除 {removed} 行；相同源码保持中性背景。")
                components.html(build_side_by_side_html(before, after, before_label="初稿", after_label="最终"), height=650, scrolling=True)
            else:
                st.info("报告没有完整的两份源码，不能展示或推测修改。")
            _render_checks(entry)
            unresolved = (entry.get("snapshot") or {}).get("unresolved") or []
            if unresolved:
                st.markdown("**仍待确认**")
                for item in unresolved:
                    st.write(f"• {item}")
    st.subheader("实际用量")
    usage = st.columns(3)
    usage[0].metric("真实 API 调用", totals["actual_model_calls"])
    usage[1].metric("真实模型 token", totals["actual_tokens"])
    usage[2].metric("有效执行时间（秒）", f"{totals['active_seconds']:.3f}")
    if not real:
        st.caption(f"离线固定产物的模拟角色调用 {totals['scripted_model_calls']} 次，与真实 API 调用分开统计。")
    if report.get("budget_overrun_observed"):
        st.warning("记录显示实际用量超过计划预算，请以实际统计为准。")
    st.download_button("下载当前评测报告", data=json.dumps(report, ensure_ascii=False, indent=2).encode("utf-8"),
                       file_name=f"codelens-evaluation-{'live' if real else 'offline'}.json", mime="application/json", key="flow_eval_download")


__all__ = ["audit_evidence", "evaluation_totals", "load_saved_report", "render_evaluation_page", "source_codes"]
