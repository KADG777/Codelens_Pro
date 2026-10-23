"""CodeLens Flow: a Chinese workspace for the Homework 2 agent workflow."""

import os
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import streamlit as st
import streamlit.components.v1 as components

from review_agent.config import MAX_SOURCE_BYTES, MAX_SOURCE_LINES, ROOT, Settings
from review_agent.files import decode_source
from ui.diff_view import build_side_by_side_html, diff_counts
from ui.evidence import issue_traces, report_markdown
from ui.execution_graph import build_execution_svg, render_node_details
from ui.progress import (
    branch_overlap_evidence,
    build_progress_html,
    build_summary_html,
    usage_summary,
)
from ui.workflow_editor import DEFAULT_PLAN, render_workflow_editor, saved_editor_plan
from workflow import WorkflowRunService
from workflow.examples import BUGGY_ORDERS, CORRECT_ORDERS, ORDER_TASK
from workflow.models import requires_measured_evidence
from workflow.registry import list_task_specs
from workflow.validation import validate_configuration

STATUS = {
    "created": "等待开始",
    "running": "协作进行中",
    "paused": "已暂停，可继续",
    "blocked": "等待处理",
    "needs_attention": "仍有问题待处理",
    "completed_static": "静态检查通过",
    "completed_verified": "功能验收通过",
    "failed": "运行中断",
}
PHASE = {
    "created": "等待开始",
    "create": "创建任务",
    "coding": "编码 Agent 生成源码",
    "review": "审查 Agent 检查问题",
    "validate": "独立检查与验收",
    "validation": "独立检查与验收",
    "fix": "修复 Agent 更新源码",
    "repair": "修复 Agent 更新源码",
    "finish": "整理交付结果",
    "done": "流程结束",
    "gate": "判断是否需要继续修复",
    "resume": "恢复已保存的进度",
    "inspect": "并行检查汇合",
    "retry_repair": "建立关联修复任务",
}
CHECK_STATUS = {"passed": "通过", "failed": "未通过", "error": "检查出错", "skipped": "未执行"}
SCENARIOS = {"订单修复闭环": "orders", "首次检查通过": "clean", "修复无进展": "unresolved"}
SEVERITY = {"critical": "严重", "high": "高", "medium": "中", "low": "低", "info": "提示"}
REASONS = {
    "invalid_configuration": "验收配置不匹配，请调整合同或验证档位后新建任务",
    "budget_exhausted": "已达到运行预算，保留现有结果",
    "invalid_output": "角色输出不完整或不符合要求，保留之前的源码",
    "runtime_configuration": "需要检查运行配置后继续",
    "runtime_error": "运行环境出现错误，可处理后继续",
    "validation_environment": "验收环境尚不可用，可配置后继续",
    "no_progress": "修复没有产生源码变化，已停止重复尝试",
    "repeated_code": "候选源码重复历史版本，已停止循环",
    "stale_evidence": "检查结果对应旧源码，不能判定通过",
    "fix_budget_exhausted": "已达到最多修复轮数，仍有问题未解决",
    "unresolved_review": "仍有审查意见或需求核对结果待确认",
    "functional_not_verified": "尚未完成所需的功能验收",
    "frozen_acceptance_passed": "当前源码已通过固定的内置验收用例",
    "static_only_functionality_unverified": "本次执行了静态检查，功能仍需验收",
    "repair_disabled": "本次计划关闭修复循环，已保留检查结果与源码",
}
UPLOAD_TASK = "在保留现有代码原意、公开接口和无关行为的前提下，审查并直接修复证据充分的明确缺陷。结合审查意见处理异常与边界条件；不将源码注释中的历史做法当作不可修改的用户要求。遇到无法确认的业务意图时说明原因，不自行编造需求。"


def _version(snapshot: dict, version_id: str | None) -> dict:
    return next((v for v in snapshot.get("versions", []) if v.get("id") == version_id), {})


def _display_time(value: str) -> str:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=8))).strftime("%m-%d %H:%M:%S")
    except (ValueError, TypeError, AttributeError):
        return str(value)


def _new_task(*, preserve_contract: bool = False) -> None:
    st.session_state.pop("flow_active_run", None)
    st.session_state.pop("flow_query_error", None)
    if "run" in st.query_params:
        del st.query_params["run"]
    st.session_state.flow_query_seen = ""
    if not preserve_contract:
        st.session_state.pop("flow_acceptance_id", None)


def _activate_run(run_id: str) -> None:
    st.session_state.flow_active_run = run_id
    st.session_state.flow_query_seen = run_id
    st.session_state.pop("flow_query_error", None)
    st.query_params["run"] = run_id


def _load_run_link(service: WorkflowRunService) -> None:
    run_id = str(st.query_params.get("run", "")).strip()
    if st.session_state.get("flow_query_seen") == run_id:
        return
    st.session_state.flow_query_seen = run_id
    st.session_state.pop("flow_query_error", None)
    if not run_id:
        return
    try:
        if len(run_id) > 128:
            raise ValueError("无效的运行编号。")
        service.get_snapshot(run_id)
    except ValueError:
        st.session_state.pop("flow_active_run", None)
        st.session_state.flow_query_error = "链接中的运行记录不存在，请从历史运行中选择记录，或新建任务。"
    else:
        st.session_state.flow_active_run = run_id


def _load_example(scenario: str, acceptance_id: str = "orders-v1") -> None:
    spec = next((item for item in list_task_specs() if item["id"] == acceptance_id), {})
    task = spec.get("task", ORDER_TASK)
    st.session_state.update(
        flow_task=task,
        flow_source=spec.get("correct_code", CORRECT_ORDERS) if scenario == "clean" else spec.get("buggy_code", BUGGY_ORDERS),
        flow_filename=spec.get("filename", "solution.py"),
        flow_acceptance_id=spec.get("id", "orders-v1"),
        flow_task_explicit=False,
        flow_task_auto_value=task,
    )
    _new_task(preserve_contract=True)


def _edit_as_new(code: str, task: str, filename: str, acceptance_id: str | None = None) -> None:
    st.session_state.update(
        flow_entry="上传 / 粘贴代码",
        flow_source_method="粘贴代码",
        flow_source=code,
        flow_task=task,
        flow_task_explicit=True,
        flow_filename=filename,
        flow_acceptance_id=acceptance_id if acceptance_id != "none" else None,
        flow_notice="已将当前源码放入新任务。修改后重新提交，旧版本的审查结果不会用于新代码。",
    )
    _new_task(preserve_contract=True)


def _mark_task_explicit() -> None:
    st.session_state.flow_task_explicit = True


def _clear_contract() -> None:
    st.session_state.pop("flow_acceptance_id", None)
    if not st.session_state.get("flow_task_explicit"):
        desired = UPLOAD_TASK if st.session_state.get("flow_entry") == "上传 / 粘贴代码" else ""
        st.session_state.flow_task = desired
        st.session_state.flow_task_auto_value = desired


def _prepare_task_default(mode: str, source_mode: bool) -> None:
    """Change only untouched defaults when the entry changes, never user goals."""
    bound = next((s for s in list_task_specs() if s["id"] == st.session_state.get("flow_acceptance_id")), {})
    desired = bound.get("task") or (UPLOAD_TASK if mode == "live" and source_mode else ORDER_TASK)
    if "flow_task" not in st.session_state:
        st.session_state.flow_task = desired
        st.session_state.flow_task_explicit = False
        st.session_state.flow_task_auto_value = desired
    elif not st.session_state.get("flow_task_explicit", False):
        previous = st.session_state.get("flow_task_auto_value", ORDER_TASK)
        if st.session_state.flow_task == previous:
            st.session_state.flow_task = desired
            st.session_state.flow_task_auto_value = desired


def _sidebar(service: WorkflowRunService) -> tuple[str, Settings, int, str, str, bool]:
    try:
        defaults = Settings.from_env()
    except ValueError:
        defaults = Settings()
    with st.sidebar:
        st.markdown("### CodeLens Flow")
        st.caption("编码 → 审查 → 修复 → 再验证")
        label = st.radio("运行方式", ["流程演示（无需密钥）", "真实模型"], key="flow_mode")
        mode = "demo" if label.startswith("流程演示") else "live"
        if mode == "demo":
            st.caption("新任务将使用预设流程；该模式的模型调用和用量均为 0。历史记录按原运行方式展示。")
        with st.expander("真实模型连接", expanded=mode == "live"):
            secret = st.text_input("API Key", type="password", key="flow_api_key")
            if defaults.api_key:
                st.caption("已检测到本机配置；留空可使用该配置。")
            model = st.text_input("模型名称", value=defaults.model, key="flow_model")
            url = st.text_input("API 基础地址", value=defaults.base_url, key="flow_base_url")
            timeout = st.number_input("等待时间（秒）", min_value=5, max_value=120, value=int(defaults.timeout), key="flow_timeout")
            st.caption("开始后将把任务和源码发送到配置的模型服务。密钥仅用于运行，不写入任务记录。")
        # Runtime connection settings also serve restored live tasks, independently
        # of the mode selected for the next new task.
        settings = replace(defaults, api_key=secret.strip() or defaults.api_key, model=model.strip(), base_url=url.strip().rstrip("/"), timeout=float(timeout))
        st.divider()
        scenario_label = st.selectbox("演示场景", list(SCENARIOS), key="flow_scenario", disabled=mode != "demo")
        profiles = {"仅静态检查": "static_only", "隔离功能验收（便携运行时）": "functional_portable", "内置订单功能验收（需 Docker）": "functional_required"}
        profile_label = st.selectbox("验收方式", list(profiles), key="flow_profile")
        profile = profiles[profile_label]
        st.caption("便携隔离验收无需 Docker，仅执行已注册合同的用例；Docker 档需要独立环境。静态通过不代表功能正确。")
        plan = saved_editor_plan()
        rounds, pause = plan["max_fix_rounds"], plan["pause_after_review"]
        st.caption("顺序 / 并行、修复循环和暂停可在主界面的流程编排区配置。")
        st.divider()
        st.markdown("**历史运行**")
        runs = service.list_runs()
        if runs:
            run_map = {str(r.get("run_id", r.get("id", ""))): r for r in runs}

            def label_run(run_id: str) -> str:
                run = run_map[run_id]
                task = str(run.get("task", "未命名任务")).replace("\n", " ")[:22]
                return f"{task} · {STATUS.get(run.get('status'), run.get('status', ''))} · {run_id[:6]}"

            selected = st.selectbox("选择记录", list(run_map), format_func=label_run, key="flow_history")
            if st.button("打开记录", key="flow_open_history", use_container_width=True):
                _activate_run(selected)
        else:
            st.caption("完成或暂停一次运行后，记录会显示在这里。")
        st.button("新建任务", on_click=_new_task, key="flow_new_task", use_container_width=True)
        st.caption("Python 单文件自动修复 · 原版 Python / C++ 审查可从左侧页面进入。")
    return mode, settings, int(rounds), profile, SCENARIOS[scenario_label], pause


def _execute(service: WorkflowRunService, run_id: str, settings: Settings, *, pause: bool = False, progress_slot=None) -> None:
    live_progress = progress_slot if progress_slot is not None else st.empty()

    def on_event(snapshot: dict) -> None:
        live_progress.markdown(build_execution_svg(snapshot.get("workflow_config", {}), snapshot) + build_progress_html(snapshot), unsafe_allow_html=True)

    with st.spinner("Agent 正在协作，已完成的节点会自动保存……"):
        snapshot = service.get_snapshot(run_id)
        on_event(snapshot)
        # The saved plan owns pauses, including the parallel join. Legacy runs
        # without a plan retain the original explicit review pause behavior.
        stop = "review" if pause and not snapshot.get("workflow_config") else None
        service.execute(run_id, settings=settings, on_event=on_event, stop_after=stop)


def _input_workspace(service: WorkflowRunService, mode: str, settings: Settings, rounds: int, profile: str, scenario: str, pause: bool) -> None:
    with st.container(border=True):
        st.subheader("任务与源码")
        plan = render_workflow_editor()
        entry = st.radio("任务入口", ["从需求生成", "上传 / 粘贴代码"], horizontal=True, key="flow_entry")
        source_mode = entry == "上传 / 粘贴代码"
        st.caption("审查 Agent → 修复 Agent → 再审查与验证" if source_mode else "编码 Agent → 审查 Agent → 修复 Agent → 再审查与验证")
        if mode == "demo":
            st.caption("预设编码和修复仅适用于带演示标记的样例；任意需求和源码请使用真实模型。")
        specs = list_task_specs()
        by_id = {spec["id"]: spec for spec in specs}
        chosen_spec = st.selectbox("任务示例与验收合同", list(by_id), format_func=lambda spec_id: by_id[spec_id]["label"] + (" · 可演示" if by_id[spec_id].get("demo_available") else " · 真实模型"), key="flow_task_spec")
        st.button("载入当前场景示例", on_click=_load_example, args=(scenario, chosen_spec), key="flow_load_example")
        _prepare_task_default(mode, source_mode)
        task = st.text_area("本次目标 / 验收要求", height=125, key="flow_task", on_change=_mark_task_explicit, help="说明输入、输出和边界条件。真实上传默认保留代码原意并修复明确缺陷；你填写的显式目标会保留。")
        acceptance_id = st.session_state.get("flow_acceptance_id") or next((spec["id"] for spec in specs if spec["task"].strip() == task.strip()), None)
        if acceptance_id in by_id:
            st.caption(f"本次验收合同：{by_id[acceptance_id]['label']} · 公开接口 {by_id[acceptance_id]['entrypoint']}；目标文字可修改，验收合同保持固定。")
            if st.session_state.get("flow_acceptance_id"):
                st.button("解除内置合同绑定", on_click=_clear_contract, key="flow_clear_contract")
        else:
            st.caption("当前为自定义目标；静态检查可用，功能验收需要显式载入已注册的合同。")
        source = None
        filename = "solution.py"
        valid_source = True
        if source_mode:
            method = st.radio("代码来源", ["粘贴代码", "上传文件"], horizontal=True, key="flow_source_method")
            if method == "上传文件":
                uploaded = st.file_uploader("上传 Python 源码", type=["py"], key="flow_upload", on_change=_clear_contract, help="修复工作副本；保留上传快照。浏览器不会覆盖电脑上的原文件。")
                if uploaded:
                    filename = Path(uploaded.name).name
                    try:
                        source = decode_source(uploaded.getvalue(), filename)
                        st.code(source, language="python", line_numbers=True)
                    except ValueError as error:
                        st.error(str(error))
                        valid_source = False
                else:
                    st.caption("选择文件后即可开始；也可以切换到粘贴代码。")
            else:
                st.session_state.setdefault("flow_filename", "solution.py")
                filename = st.text_input("文件名", key="flow_filename")
                initial_source = "" if mode == "live" else (CORRECT_ORDERS if scenario == "clean" else BUGGY_ORDERS)
                st.session_state.setdefault("flow_source", initial_source)
                source = st.text_area("Python 源码", height=280, key="flow_source")
            st.caption("修复后的完整源码会成为当前工作版本。原始源码保留，可在下方查看红绿差异并下载。")
        button_label = "开始审查与修复" if source_mode else "开始生成、审查与修复"
        start = st.button(button_label, type="primary", key="flow_start", use_container_width=True)
        if start:
            try:
                if not task.strip():
                    raise ValueError("请先填写本次目标和验收要求。")
                if source_mode and (not valid_source or not source or not source.strip()):
                    raise ValueError("请上传或粘贴需要审查的 Python 源码。")
                if not filename.lower().endswith(".py") or Path(filename).name != filename:
                    raise ValueError("请使用不含目录的 .py 文件名。")
                if source is not None and (len(source.encode("utf-8")) > MAX_SOURCE_BYTES or len(source.splitlines()) > MAX_SOURCE_LINES):
                    raise ValueError(f"请使用不超过 {MAX_SOURCE_BYTES // 1000} KB、{MAX_SOURCE_LINES} 行的源码。")
                validate_configuration(profile, acceptance_id or "none")
                if mode == "live":
                    settings.validate()
                elif acceptance_id in by_id and not by_id[acceptance_id].get("demo_available"):
                    raise ValueError("这个任务合同没有预设角色响应，请切换真实模型运行；也可选择带“可演示”标记的样例。")
                run_id = service.create_run(task.strip(), source=source, filename=filename, mode=mode, max_fix_rounds=plan["max_fix_rounds"], validation_profile=profile, scenario=scenario, acceptance_id=acceptance_id or "none", workflow_config=plan)
                _activate_run(run_id)
                _execute(service, run_id, settings, pause=pause)
            except ValueError as error:
                st.error(str(error))
            except Exception:
                st.error("运行暂时中断。已保存的进度可从历史运行中打开；请检查连接后继续。")
            else:
                st.rerun()


def _render_issue_trace(snapshot: dict) -> dict | None:
    traces = issue_traces(snapshot)
    if not traces:
        return None
    with st.container(border=True):
        st.markdown("**问题 / 失败用例 → 修复版本 → 重新验收**")
        options = {trace["key"]: trace for trace in traces}
        selected = st.selectbox("定位一个审查问题或失败用例", [""] + list(options),
                                format_func=lambda key: options[key]["label"] if key else "查看全部改动（不锁定单条问题）",
                                key=f"flow_issue_{snapshot['run_id']}")
        st.caption(f"记录了 {len(traces)} 条绑定源码版本的问题。模型声明处理、复查未再检出与实际用例通过分别展示。")
        if not selected:
            return None
        trace = options[selected]
        finding = trace["finding"]
        columns = st.columns(3)
        with columns[0]:
            st.markdown("**① 失败用例**" if trace.get("kind") == "validation" else "**① 审查定位**")
            location = f"第 {finding['line_start']}–{finding.get('line_end', finding['line_start'])} 行" if finding.get("line_start") else "独立验收记录（无源码行定位）"
            st.write(f"{finding.get('id', '?')} @ {trace['source']['label']} · {location}")
            st.caption(f"源码标识 {trace['source']['hash'][:12]}")
            st.write(finding.get("explanation", finding.get("title", "")))
            if trace.get("kind") == "validation":
                st.json({key: trace["original_check"][key] for key in ("expected", "actual") if key in trace["original_check"]}, expanded=True)
        with columns[1]:
            st.markdown("**② 修复 Agent 声明**")
            st.write(trace["record"].get("change_summary") or "没有声明已处理这条问题。")
            if trace["target"]:
                st.caption(f"已保存版本：{trace['source']['label']} → {trace['target']['label']}")
            if trace.get("link_basis") == "lineage":
                st.caption("按相邻版本关联；修复 Agent 未声明处理该用例，是否解决以重新验收为准。")
            if trace["near_blocks"]:
                labels = [f"修改块 {b['number']}" for b in trace["near_blocks"]]
                st.caption("与问题定位行相交：" + "、".join(labels) + "（位置关联，不代表修复正确）")
            elif trace.get("kind") == "validation":
                st.caption("该用例没有源码行号；完整差异展示实际修改，重新验收判断用例是否解决。")
            else:
                st.caption("没有可核对的定位行修改块，不能据模型声明推断问题已修复。")
        with columns[2]:
            st.markdown("**③ 复查与验证证据**")
            st.write(trace["review_status"])
            st.caption(trace["validation_status"])
            if trace.get("retest"):
                st.caption("重新验收记录：" + trace["retest"].get("detail", trace["retest"].get("status", "")))
        if finding.get("evidence"):
            with st.expander("查看这条问题的原始证据"):
                st.code(finding["evidence"], language="python")
        return trace


def _render_code(snapshot: dict, trace: dict | None = None) -> None:
    versions = snapshot.get("versions", [])
    current = _version(snapshot, snapshot.get("current_version"))
    original = _version(snapshot, snapshot.get("original_version"))
    if not current:
        st.info("还没有源码版本。开始或继续任务后，生成的源码会显示在这里。")
        return
    original = original or versions[0]
    code = str(current.get("code", ""))
    name = snapshot.get("filename", "solution.py")
    with st.container(border=True):
        st.subheader("代码工作区")
        st.caption(f"{name} · 当前版本 {current.get('label', current.get('id', ''))} · 已保存 {len(versions)} 个版本")
        no_progress = snapshot.get("termination_reason") in {"no_progress", "repeated_code"}
        repairs = snapshot.get("repair_records") or []
        if no_progress:
            st.warning("本轮修复未生效：没有保存新的源码版本。下面仍展示真实的修改前后源码。")
            if repairs:
                last_repair = repairs[-1]
                st.write("**最近一次修复的说明**")
                st.write(last_repair.get("change_summary") or "修复结果没有产生可提交的源码变化。")
                for item in last_repair.get("unresolved_items") or []:
                    st.write(f"• {item}")
            for item in snapshot.get("unresolved") or []:
                st.caption(str(item))
        st.markdown("**修改前 / 修改后**")
        compare = st.radio("对比范围", ["首次上传 / 生成 → 当前", "最近一轮修复前 → 当前", "选择历史版本"], horizontal=True, key=f"flow_compare_{snapshot['run_id']}", disabled=trace is not None)
        baseline, display = original, current
        if compare == "最近一轮修复前 → 当前":
            baseline = current if no_progress else (_version(snapshot, current.get("parent")) or original)
            if no_progress:
                st.caption("最近一次修复未产生新版本，因此本轮改前和改后均为当前保留版本。")
        elif compare == "选择历史版本":
            ids = [str(v["id"]) for v in versions]
            by_id = {str(v["id"]): v for v in versions}
            chosen = st.selectbox("选择改前版本", ids, format_func=lambda version_id: str(by_id[version_id].get("label", version_id)), key=f"flow_baseline_{snapshot['run_id']}")
            baseline = by_id[chosen]
        if trace:
            baseline = trace["source"]
            display = trace["target"] or trace["source"]
            st.info(f"正在定位 {trace['label']}：{baseline['label']} → {display['label']}。当前可下载的工作源码仍为 {current.get('label', '')}。")
        full = st.checkbox("展开未修改的代码", key=f"flow_full_diff_{snapshot['run_id']}")
        display_code = str(display.get("code", ""))
        added, deleted = diff_counts(str(baseline.get("code", "")), display_code)
        st.caption(f"新增 {added} 行 · 删除 {deleted} 行 · 红色 − 是改前内容，绿色 + 是改后内容，未改代码保持普通背景")
        if not added and not deleted and not no_progress:
            if trace:
                st.info("所选问题尚无可关联的新改动，下面保留对应源码供核对。")
            elif snapshot.get("fix_round", 0):
                st.info("所选两个版本的源码相同，没有可标红或标绿的修改。")
            elif snapshot.get("status") in {"completed_static", "completed_verified"}:
                st.info("本次检查没有触发源码修复；两侧保留相同源码，未标记为已修改。")
            else:
                st.info("尚未产生修复版本，两侧显示当前保留的源码。")
        focus = (int(trace["finding"]["line_start"]), int(trace["finding"].get("line_end", trace["finding"]["line_start"]))) if trace and trace["finding"].get("line_start") else None
        components.html(build_side_by_side_html(str(baseline.get("code", "")), display_code, before_label=str(baseline.get("label", "改前版本")), after_label=str(display.get("label", "当前版本")), context=None if full else 3, focus_old_range=focus), height=650, scrolling=True)
        with st.expander("查看并复制当前完整源码"):
            st.code(code, language="python", line_numbers=True)
            st.caption("可使用代码区右上角按钮复制。手动编辑会开启新任务并重新审查。")
        with st.expander("查看原始源码快照"):
            st.code(str(original.get("code", "")), language="python", line_numbers=True)
            st.caption("这是首次上传或首次生成的快照，后续修复不会覆盖它。")
        download, edit = st.columns(2)
        with download:
            st.download_button("下载当前完整源码", data=code.encode("utf-8"), file_name=Path(name).name, mime="text/x-python", key=f"flow_download_{snapshot['run_id']}", use_container_width=True)
        with edit:
            st.button("以此代码继续编辑", on_click=_edit_as_new, args=(code, snapshot.get("task", ""), name, snapshot.get("acceptance_id")), key=f"flow_edit_{snapshot['run_id']}", use_container_width=True)


def _render_review(snapshot: dict) -> None:
    review = snapshot.get("review") or {}
    current = _version(snapshot, snapshot.get("current_version"))
    if not review:
        st.caption("完成审查节点后，会显示当前源码对应的问题清单。")
        return
    if review.get("code_hash") != current.get("hash"):
        st.warning("这份审查对应之前的版本；当前源码需要重新审查。")
    st.write(review.get("summary") or "审查已保存。")
    findings = review.get("findings") or []
    pending = review.get("pending_findings") or []
    if review.get("complete") and not findings and not pending:
        st.success("当前审查未发现待修复问题。")
    for number, finding in enumerate(findings, 1):
        severity = SEVERITY.get(str(finding.get("severity", "")).lower(), str(finding.get("severity", "问题")))
        title = finding.get("title") or finding.get("message") or f"问题 {number}"
        with st.expander(f"{number}. [{severity}] {title}", expanded=True):
            for key, label in [("explanation", "问题"), ("description", "说明"), ("evidence", "证据"), ("suggestion", "修复建议"), ("trigger", "触发条件")]:
                if finding.get(key):
                    st.write(f"**{label}**")
                    st.write(finding[key])
            line = finding.get("line_start", finding.get("line"))
            if line:
                st.caption(f"定位：第 {line} 行")
    if pending:
        st.warning(f"还有 {len(pending)} 条待确认意见，不能据此宣称全部通过。")
        for item in pending:
            with st.expander(str(item.get("title", "待确认意见"))):
                st.write(item.get("explanation") or item.get("reason") or "请核对原始条件和证据。")
    if review.get("requirements_status"):
        requirements = {"met": "已满足显式要求", "gap": "发现需求差距", "unknown": "尚无法确认", "not_evaluated": "预设流程未进行通用需求语义判断"}
        requirement_status = review["requirements_status"]
        unmeasured_history = requirement_status == "met" and requires_measured_evidence(snapshot.get("task", ""))
        if unmeasured_history:
            requirement_status = "unknown"
            st.warning("历史需求判断仅依据源码，未实测准确率或性能；当前不能确认目标已达成。原始判断保留在历史记录中。")
        st.caption(f"需求核对：{requirements.get(requirement_status, requirement_status)}")
        if review.get("requirements_reason") and not unmeasured_history:
            st.caption(review["requirements_reason"])


def _render_validation(snapshot: dict) -> None:
    validation = snapshot.get("validation") or {}
    if not validation:
        st.caption("独立检查完成后，会在这里展示真实检查结果。")
        return
    current = _version(snapshot, snapshot.get("current_version"))
    if validation.get("code_hash") != current.get("hash"):
        st.warning("这份检查结果对应之前的版本，不能代表当前源码。")
    st.write(f"检查结果：{CHECK_STATUS.get(validation.get('status'), validation.get('status', '待检查'))}")
    if validation.get("functional_executed"):
        st.caption("本次已执行内置功能验收；请结合逐项结果判断。")
    else:
        st.info("本次尚未执行功能验收。静态检查通过不等于功能正确。")
    checks = validation.get("checks") or []
    if checks:
        rows = [{"检查项": item.get("name", item.get("id", "")), "结果": CHECK_STATUS.get(item.get("status"), item.get("status", "")), "说明": str(item.get("detail", ""))} for item in checks]
        st.dataframe(rows, hide_index=True, use_container_width=True)
        with st.expander("查看验收记录"):
            st.json(checks)


def _render_run(service: WorkflowRunService, snapshot: dict, settings: Settings, max_fix_rounds: int = 2) -> None:
    status = snapshot.get("status", "created")
    st.subheader("本次运行")
    st.caption(f"任务编号 {snapshot['run_id']} · {'预设流程演示' if snapshot.get('mode') == 'demo' else '真实模型'}")
    st.caption(f"[打开此运行的记录链接](?run={quote(snapshot['run_id'], safe='')}) · 链接只在保存这些记录的本机服务上可用。")
    st.caption(f"最近环节：{PHASE.get(snapshot.get('phase'), snapshot.get('phase', '等待开始'))}")
    with st.expander("本次目标 / 验收要求"):
        st.write(snapshot.get("task", ""))
    if snapshot.get("mode") == "demo":
        st.info("这份记录使用预设角色输出，展示流程和恢复机制；不代表真实模型的修复效果。")
    st.markdown(build_summary_html(snapshot), unsafe_allow_html=True)
    progress_slot = st.empty()
    progress_slot.markdown(build_execution_svg(snapshot.get("workflow_config", {}), snapshot, selected=st.query_params.get("node", "")) + build_progress_html(snapshot), unsafe_allow_html=True)
    render_node_details(snapshot)
    text = STATUS.get(status, status)
    raw_reason = snapshot.get("termination_reason") or ""
    reason = REASONS.get(raw_reason, raw_reason) or snapshot.get("error")
    if status == "completed_verified":
        st.success(f"{text}" + (f" · {reason}" if reason else ""))
    elif status == "completed_static":
        st.warning(f"{text}；本次未完成完整功能验收。" + (f" {reason}" if reason else ""))
    elif status in {"needs_attention", "blocked", "failed"}:
        st.warning(f"{text}" + (f" · {reason}" if reason else ""))
    else:
        st.info(f"{text}" + (f" · {reason}" if reason else ""))
    metrics = snapshot.get("metrics") or {}
    parent = snapshot.get("parent_run_id") or snapshot.get("retry_of")
    if parent:
        st.info("本次为关联修复：继承源码与历史记录，使用独立的新预算；下方已区分本轮与继承来源。")
        st.caption(f"来源运行：[打开 {parent[:10]}](?run={quote(parent, safe='')}) · 历史记录不变，本轮产物仍需重新审查和验收。")
    with st.expander("本次保存的编排计划与用量"):
        plan = {**DEFAULT_PLAN, **snapshot["workflow_config"]} if snapshot.get("workflow_config") else {**DEFAULT_PLAN, "max_fix_rounds": snapshot.get("max_fix_rounds", 2)}
        st.write(f"检查方式：{'并行分支，完成后汇合' if plan['parallel_checks'] else '顺序执行'} · 修复循环：{'开启' if plan['enable_repair'] else '关闭'} · 最多 {plan['max_fix_rounds']} 轮 · 审查后暂停：{'开启' if plan['pause_after_review'] else '关闭'}")
        st.caption("这是该运行已保存的计划，修改新任务的编排不会改变本记录。")
        st.caption(f"验收合同：{snapshot.get('acceptance_id', '自定义目标')}")
        overlaps = branch_overlap_evidence(snapshot)
        if overlaps:
            st.caption(f"实际记录到 {len(overlaps)} 组同版本分支重叠，最大重叠 {max(item['overlap_seconds'] for item in overlaps):.3f} 秒；这是实际执行时间的交集。")
        columns = st.columns(4)
        columns[0].metric("已保存版本", len(snapshot.get("versions", [])))
        columns[1].metric("修复轮数", f"{snapshot.get('fix_round', 0)} / {snapshot.get('max_fix_rounds', 0)}")
        columns[2].metric("模型调用", metrics.get("model_calls", 0), help="仅统计本轮，不含继承记录。")
        columns[3].metric("模型用量", int(metrics.get("prompt_tokens", 0)) + int(metrics.get("completion_tokens", 0)), help="本轮输入与输出 token 合计；流程演示为 0。")
    if snapshot.get("error") and snapshot.get("error") != reason:
        st.caption(snapshot["error"])
    can_resume = snapshot.get("recoverable") or (status in {"created", "paused"} and bool(snapshot.get("pending_nodes")))
    if can_resume:
        st.caption("继续时使用此任务已保存的配置和源码。真实模型任务请先在侧栏填写可用的密钥。")
        if st.button("继续此任务", type="primary", key="flow_resume"):
            try:
                if snapshot.get("mode") == "live":
                    settings.validate()
                _execute(service, snapshot["run_id"], settings, progress_slot=progress_slot)
            except ValueError as error:
                st.error(str(error))
            except Exception:
                st.error("暂时未能继续。请检查连接；已经保存的源码和进度仍可查看。")
            else:
                st.rerun()
    retryable = (
        snapshot.get("mode") == "live"
        and status == "needs_attention"
        and snapshot.get("termination_reason") in {"no_progress", "repeated_code", "fix_budget_exhausted"}
        and bool(snapshot.get("review"))
        and bool(snapshot.get("validation"))
    )
    if retryable:
        retry_rounds = max(1, max_fix_rounds)
        st.caption(f"重新修复会保留本次目标、源码和已有审查，创建关联的新任务，最多修复 {retry_rounds} 轮并重新计算运行预算；原记录保留。")
        if st.button("按审查意见重新修复", type="primary", key="flow_retry_repair"):
            try:
                settings.validate()
                retry_id = service.retry_repair(snapshot["run_id"], max_fix_rounds=retry_rounds)
                _activate_run(retry_id)
                _execute(service, retry_id, settings, progress_slot=progress_slot)
            except ValueError as error:
                st.error(str(error))
            except Exception:
                st.error("暂时未能重新修复。原记录仍然保留；若已创建新任务，可从历史运行中打开继续。")
            else:
                st.rerun()
    selected_trace = _render_issue_trace(snapshot)
    _render_code(snapshot, selected_trace)
    st.download_button("下载本次运行报告", data=report_markdown(snapshot).encode("utf-8"), file_name=f"codelens-{snapshot['run_id'][:10]}.md", mime="text/markdown", key=f"flow_report_{snapshot['run_id']}")
    review_tab, validate_tab, event_tab, history_tab = st.tabs(["审查结果", "独立验证", "协作记录", "版本与用量"])
    with review_tab:
        _render_review(snapshot)
        review_history = snapshot.get("review_history") or []
        if len(review_history) > 1:
            with st.expander("查看修复前的审查与证据"):
                for history in review_history[:-1]:
                    version = next((v for v in snapshot.get("versions", []) if v.get("hash") == history.get("code_hash")), {})
                    st.write(f"**{version.get('label', '历史版本')} · 审查 Agent**")
                    st.write(history.get("summary", ""))
                    for finding in history.get("findings", []):
                        st.write(f"{finding.get('id', '')} · {finding.get('title', '')}")
                        if finding.get("evidence"):
                            st.code(finding["evidence"], language="python")
    with validate_tab:
        _render_validation(snapshot)
    with event_tab:
        st.caption("需求入口由三个角色协作；源码入口直接进入审查。每轮修复后重新检查。")
        overlaps = branch_overlap_evidence(snapshot)
        if overlaps:
            st.markdown("**并行执行证据**")
            st.dataframe([{"源码标识": item["code_hash"][:12], "审查耗时（秒）": round(item["review_seconds"], 3), "验收耗时（秒）": round(item["validation_seconds"], 3), "实际重叠（秒）": round(item["overlap_seconds"], 3)} for item in overlaps], hide_index=True, use_container_width=True)
        events = snapshot.get("events") or []
        if events:
            rows = [{"来源": f"继承 · {str(event.get('source_run_id', parent or ''))[:8]}" if event.get("inherited") else "本轮", "时间（北京时间）": _display_time(event.get("time", "")), "环节": PHASE.get(event.get("node"), event.get("node", "")), "状态": STATUS.get(event.get("status"), event.get("status", "")), "说明": REASONS.get(event.get("message"), event.get("message", "")), "版本": event.get("version", "")} for event in events]
            st.dataframe(rows, hide_index=True, use_container_width=True)
        else:
            st.caption("尚无执行记录。")
        for repair in snapshot.get("repair_records") or []:
            source = f"继承自 {str(repair.get('source_run_id', parent or ''))[:8]}" if repair.get("inherited") else "本轮"
            with st.expander(f"{source} · 修复 Agent · 第 {repair.get('round', '?')} 轮交接", expanded=not repair.get("inherited")):
                st.write(repair.get("change_summary", "完整源码已保存。"))
                st.caption(f"改前源码标识：{str(repair.get('base_code_hash', ''))[:12]} · 对应审查问题：{'、'.join(repair.get('addressed_finding_ids', [])) or '无'}")
                if repair.get("unresolved_items"):
                    st.write(repair["unresolved_items"])
    with history_tab:
        versions = snapshot.get("versions") or []
        if versions:
            origins = {"upload": "上传 / 粘贴", "coding": "编码 Agent", "fix": "修复 Agent"}
            rows = [{"版本": v.get("label", v.get("id", "")), "来源": origins.get(v.get("origin"), v.get("origin", "")), "父版本": v.get("parent", ""), "源码标识": str(v.get("hash", ""))[:12]} for v in versions]
            st.dataframe(rows, hide_index=True, use_container_width=True)
        st.caption(f"本轮有效执行时间：{float(metrics.get('active_seconds', 0)):.2f} 秒 · HTTP 请求：{metrics.get('http_attempts', 0)} 次 · 工具调用：{metrics.get('tool_calls', 0)} 次")
        usage = usage_summary(snapshot)
        usage_rows = []
        for key, label in [("current", "本轮"), ("inherited", "已记录的此前来源"), ("total", "已记录来源合计")]:
            item = usage[key]
            usage_rows.append({"范围": label, "模型调用": item["model_calls"], "输入 + 输出 token": item["prompt_tokens"] + item["completion_tokens"], "有效执行秒数": round(item["active_seconds"], 2)})
        st.dataframe(usage_rows, hide_index=True, use_container_width=True)
        if snapshot.get("mode") == "demo":
            st.caption(f"预设角色动作：{metrics.get('scripted_actions', 0)} 次；与真实模型调用分开计数。")
        if snapshot.get("unresolved"):
            st.warning("未解决事项")
            st.write(snapshot["unresolved"])


def main() -> None:
    st.set_page_config(page_title="CodeLens Flow · 协作修复工作区", page_icon="🔎", layout="wide")
    st.markdown("""<style>
    .block-container {max-width:1280px;padding-top:2.2rem;padding-bottom:3rem}
    h1 {letter-spacing:-.035em} [data-testid="stMetricValue"] {font-size:1.65rem}
    [data-testid="stSidebar"] {border-right:1px solid #e2e8f0}
    textarea {font-family:Consolas,'Courier New',monospace!important}
    </style>""", unsafe_allow_html=True)
    data_dir = Path(os.environ.get("CODELENS_FLOW_DATA_DIR", str(ROOT / "data")))
    with closing(WorkflowRunService(data_dir / "workflow.sqlite3")) as service:
        _page(service)


def _page(service: WorkflowRunService) -> None:
    _load_run_link(service)
    mode, settings, rounds, profile, scenario, pause = _sidebar(service)
    st.title("CodeLens Flow")
    st.write("让编码、审查与修复衔接起来，让每一次修改都可以查看和验证。")
    if mode == "demo" and not st.session_state.get("flow_active_run"):
        st.info("当前为预设流程演示：无需 API Key，适合检查协作流程、红绿差异与恢复功能。真实模型请在侧栏切换。")
    if notice := st.session_state.pop("flow_notice", None):
        st.info(notice)
    if query_error := st.session_state.get("flow_query_error"):
        st.error(query_error)
    run_id = st.session_state.get("flow_active_run")
    if run_id:
        try:
            snapshot = service.get_snapshot(run_id)
        except ValueError as error:
            st.error(str(error))
        else:
            _render_run(service, snapshot, settings, max_fix_rounds=rounds)
    else:
        _input_workspace(service, mode, settings, rounds, profile, scenario, pause)
        st.caption("开始后将在这里展示当前源码、红绿差异、审查意见及验收结果。任务记录保存在本机，可刷新或重新打开后继续。")


if __name__ == "__main__":
    main()
