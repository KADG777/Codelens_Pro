"""Run with: python -m streamlit run app.py"""

from dataclasses import replace

import streamlit as st

from review_agent.agent import ReviewAgent
from review_agent.config import ROOT, Settings
from review_agent.files import decode_source
from review_agent.languages import LANGUAGE_LABELS, UPLOAD_EXTENSIONS, detect_language
from review_agent.llm import DeepSeekClient, ModelError
from review_agent.models import ReviewedFinding, ReviewInput, ReviewReport
from review_agent.reporting import (
    EVIDENCE_ROLES,
    SEVERITY,
    TRIGGER_BASES,
    VERIFICATION,
    to_markdown,
)

st.set_page_config(page_title="CodeLens · 代码审查助手", page_icon="🔎", layout="wide")
st.markdown(
    """
<style>
    .block-container {max-width: 1240px; padding-top: 2.5rem; padding-bottom: 3rem;}
    h1 {letter-spacing: -0.04em; font-weight: 750;}
    h2, h3 {letter-spacing: -0.02em;}
    [data-testid="stSidebar"] {border-right: 1px solid #dce4dc;}
    [data-testid="stMetricValue"] {font-size: 1.75rem;}
    textarea {font-family: Consolas, 'Courier New', monospace !important; font-size: 14px !important;}
    .eyebrow {color: #187563; letter-spacing: .16em; font-size: 12px; font-weight: 700;}
</style>
""",
    unsafe_allow_html=True,
)

try:
    defaults = Settings.from_env()
except ValueError as error:
    st.error(str(error))
    st.stop()

with st.sidebar:
    st.markdown("### 🔎 CodeLens")
    st.caption("代码审查助手 · Homework 1")
    st.divider()
    mode_label = st.radio("运行模式", ["离线演示", "DeepSeek API"], key="mode")
    is_demo = mode_label == "离线演示"
    if is_demo:
        st.info("无需 API Key。使用本地规则展示工具调用与报告流程。")
        settings = defaults
    else:
        key = st.text_input("API Key", value=defaults.api_key, type="password", key="api_key")
        with st.expander("连接设置", expanded=False):
            model_name = st.text_input("模型名称", value=defaults.model)
            base_url = st.text_input("API 基础地址", value=defaults.base_url)
        settings = replace(
            defaults, api_key=key.strip(), model=model_name.strip(), base_url=base_url.strip()
        )
        st.caption("提交后会把当前代码发送到配置的模型服务。Key 仅用于本次会话，不写入报告。")
    st.divider()
    reflection = st.toggle(
        "开启反思复核",
        value=True,
        help="逐条检查反例与触发条件。关闭或复核失败时，未经复核的模型意见进入待确认；本地证据校验始终开启。",
    )
    st.caption("Python / C++ 单文件 · 最多 800 行 / 48 KB")
    st.caption("只读分析 · 修改以建议形式呈现")
    st.divider()
    st.markdown("**审查流程**")
    st.markdown("① 输入代码\n\n② 收集工具证据\n\n③ 生成问题清单\n\n④ 复核与追问")

st.markdown('<div class="eyebrow">CODE REVIEW WORKSPACE</div>', unsafe_allow_html=True)
st.title("让每条审查意见，都有依据。")
st.write("提交 Python 或 C++ 代码，定位潜在问题，查看触发条件与修改建议。")
st.caption("本地规则演示" if is_demo else f"DeepSeek · {settings.model}")
st.divider()

if "editor" not in st.session_state:
    st.session_state.editor = (ROOT / "examples" / "buggy_order.py").read_text(encoding="utf-8")
    st.session_state.filename = "buggy_order.py"


def load_example(name: str) -> None:
    st.session_state.editor = (ROOT / "examples" / name).read_text(encoding="utf-8")
    st.session_state.filename = name


left, right = st.columns([1.6, 1], gap="large")
input_error = None
with left:
    st.subheader("01 / 提交代码")
    source_kind = st.radio(
        "代码来源", ["编辑代码", "上传文件"], horizontal=True, label_visibility="collapsed"
    )
    if source_kind == "编辑代码":
        example_language = st.selectbox("示例语言", ["Python", "C++"], key="example_language")
        example_suffix = "py" if example_language == "Python" else "cpp"
        sample1, sample2 = st.columns(2)
        sample1.button(
            "载入问题示例",
            on_click=load_example,
            args=(f"buggy_order.{example_suffix}",),
            use_container_width=True,
        )
        sample2.button(
            "载入改进示例",
            on_click=load_example,
            args=(f"clean_order.{example_suffix}",),
            use_container_width=True,
        )
        filename = st.text_input("文件名", key="filename")
        code = st.text_area("源代码", key="editor", height=360, label_visibility="collapsed")
    else:
        uploaded = st.file_uploader("上传 Python / C++ 文件", type=UPLOAD_EXTENSIONS)
        filename, code = "review.py", ""
        if uploaded is not None:
            filename = uploaded.name
            try:
                code = decode_source(uploaded.getvalue(), filename)
                st.code(code, language=detect_language(filename), line_numbers=True)
            except ValueError as error:
                input_error = str(error)
                st.error(input_error)
    try:
        detected_language = detect_language(filename)
        st.caption(
            f"识别语言：{LANGUAGE_LABELS[detected_language]} · {len(code.splitlines())} 行 · {len(code.encode('utf-8')):,} 字节"
        )
    except ValueError as error:
        input_error = str(error)
        st.error(input_error)
    st.caption("按文件后缀识别审查语言；.h 头文件按 C++ 处理。示例语言仅影响载入示例。")

with right:
    st.subheader("02 / 设置审查重点")
    focus = st.text_area(
        "希望重点关注什么？",
        height=160,
        max_chars=1000,
        value="重点检查潜在 Bug、空输入、异常处理和可维护性。请解释问题的触发条件。",
    )
    st.markdown("**你会得到**")
    st.write("带行号的问题清单、代码证据、具体修改建议，以及本次检查的覆盖范围。")
    if is_demo:
        st.caption(
            "演示规则不会理解审查重点或完整业务逻辑；例如空列表导致的平均值计算错误需要模型进一步分析。"
        )
    start = st.button(
        "开始审查 →",
        type="primary",
        use_container_width=True,
        disabled=not code.strip() or bool(input_error),
    )

if start:
    st.session_state.pop("report", None)
    st.session_state.pop("request", None)
    st.session_state.history = []
    client = None
    with st.status("正在审查代码…", expanded=True) as status:
        try:
            request = ReviewInput(code=code, filename=filename, focus=focus)
            if not is_demo:
                client = DeepSeekClient(settings)
            agent = ReviewAgent(client, model_name=settings.model)
            report = agent.review(
                request,
                strategy="full" if reflection else "tools",
                on_event=lambda event: st.write(f"**{event.stage}** · {event.detail}"),
            )
            st.session_state.report = report
            st.session_state.request = request
            status.update(label="审查完成", state="complete", expanded=False)
        except (ValueError, ModelError) as error:
            st.error(str(error))
            status.update(label="审查未完成，请检查提示后重试", state="error")
        finally:
            if client:
                client.close()


def show_finding(finding: ReviewedFinding, snapshot: ReviewInput) -> None:
    st.caption(
        f"{finding.category} · {VERIFICATION[finding.verification]}"
        + (f" · {finding.rule_id}" if finding.rule_id else "")
    )
    source_lines = snapshot.code.splitlines()
    snippet = "\n".join(
        f"{line + 1:>3}  {source_lines[line]}"
        for line in range(finding.line_start - 1, finding.line_end)
    )
    st.code(snippet, language=snapshot.language)
    st.markdown(
        "**候选说法（尚未确认）**" if finding.verification == "pending" else "**问题与触发条件**"
    )
    st.write(finding.explanation)
    st.markdown(
        f"**引用证据 · {EVIDENCE_ROLES[finding.evidence_role]} · L{finding.line_start}–L{finding.line_end}**"
    )
    st.code(finding.evidence, language=snapshot.language)
    for ref in finding.related_evidence:
        st.markdown(
            f"**补充证据 · {EVIDENCE_ROLES[ref.role]} · L{ref.line_start}–L{ref.line_end}**"
        )
        st.code(
            "\n".join(
                f"{i + 1:>3}  {source_lines[i]}" for i in range(ref.line_start - 1, ref.line_end)
            ),
            language=snapshot.language,
        )
    if finding.trigger:
        st.markdown("**具体触发条件**")
        st.write(finding.trigger)
        st.caption("条件来源：" + TRIGGER_BASES[finding.trigger_basis])
    st.markdown("**复核说明**")
    st.write(finding.review_note)
    st.markdown("**候选建议（需验证）**" if finding.verification == "pending" else "**修改建议**")
    st.write(finding.suggestion)
    if finding.verification_hint:
        st.markdown("**建议验证（未执行）**")
        st.write(finding.verification_hint)


if "report" in st.session_state:
    saved_report = st.session_state.report
    legacy_report = getattr(saved_report, "schema_version", 1) < 5
    report = ReviewReport.model_validate(saved_report.model_dump())
    snapshot = st.session_state.request
    changed = (
        code.replace("\r\n", "\n").replace("\r", "\n") != snapshot.code
        or filename != snapshot.filename
    )
    st.divider()
    st.subheader("03 / 审查报告")
    st.caption(
        f"{report.filename} · {LANGUAGE_LABELS[report.language]} · 快照 {report.source_hash} · {'离线规则演示' if report.mode == 'demo' else report.model}"
    )
    if changed:
        st.warning("编辑区内容已变化。以下报告对应上一次代码快照，请重新审查以更新结果。")
    if legacy_report:
        st.warning("这是旧版本报告；请重新审查，应用最新的事实冲突处理与追问校验。")
    displayed_findings = report.findings + report.pending_findings
    counts = {
        level: sum(item.severity == level for item in displayed_findings) for level in SEVERITY
    }
    metrics = st.columns(4)
    for column, level in zip(metrics, SEVERITY):
        column.metric(f"{SEVERITY[level]}优先级", counts[level])
    metrics[3].metric("审查耗时", f"{report.metrics.elapsed_seconds:.1f}s")
    st.write(report.summary)
    if report.reflection_status == "failed":
        st.warning("模型复核未完成；未经复核的模型意见已移到待确认。")
    st.caption(
        f"共 {len(displayed_findings)} 条问题与建议，其中 {len(report.pending_findings)} 条待确认；上方数量包含待确认条目。"
        "规则命中和模型复核均不代表已运行测试。"
    )
    withheld_count = sum(item.status == "withheld" for item in report.verification_records)
    if withheld_count:
        st.caption(
            f"另有 {withheld_count} 条候选因证据不足未展示，可在执行记录中查看原因；数量减少不代表代码问题更少。"
        )

    issues_tab, trace_tab, coverage_tab = st.tabs(
        [f"问题与建议（{len(displayed_findings)}）", "执行记录", "覆盖与限制"]
    )
    with issues_tab:
        if not displayed_findings:
            st.info("本次没有可展示的意见；这不代表代码已被证明正确。")
        level_filter = st.multiselect(
            "显示优先级",
            list(SEVERITY),
            default=list(SEVERITY),
            format_func=SEVERITY.get,
            key="level_filter",
        )
        for index, finding in enumerate(displayed_findings, 1):
            if finding.severity not in level_filter:
                continue
            with st.expander(
                f"{index:02d} · {SEVERITY[finding.severity]}优先级 · {VERIFICATION[finding.verification]} · {finding.title}  |  L{finding.line_start}–{finding.line_end}",
                expanded=index == 1,
            ):
                show_finding(finding, snapshot)
    with trace_tab:
        for event in report.events:
            st.write(f"**{event.stage}** · {event.detail}")
        st.caption(
            f"模型调用 {report.metrics.model_calls} 次 · HTTP 尝试 {report.metrics.http_attempts} 次 · "
            f"工具调用 {report.metrics.tool_calls} 次 · "
            f"输入 / 输出 token：{report.metrics.prompt_tokens} / {report.metrics.completion_tokens}"
        )
        with st.expander("查看证据校验和逐条复核记录"):
            for item in report.verification_records:
                st.write(f"**候选 {item.finding_id} · {item.title}** · {item.status}")
                st.write(item.reason)
        if report.metrics.stages:
            st.dataframe(
                [
                    {
                        "阶段": item.stage,
                        "耗时（秒）": item.elapsed_seconds,
                        "模型调用": item.model_calls,
                        "HTTP 尝试": item.http_attempts,
                        "输入 token": item.prompt_tokens,
                        "输出 token": item.completion_tokens,
                        "状态": item.status,
                    }
                    for item in report.metrics.stages
                ],
                hide_index=True,
                use_container_width=True,
            )
            st.caption("阶段耗时包含该阶段的重试等待；总耗时另含调度和界面回调开销。")
        if report.calculations:
            with st.expander("查看工具计算记录"):
                st.caption("下列参数由模型选择；计算正确不代表参数已与源码绑定。")
                st.json(report.calculations)
        if report.semantic_facts:
            with st.expander("查看切片计算与 API 依据"):
                for fact in report.semantic_facts:
                    st.write(f"**{fact['id']} · L{fact['line_start']}–L{fact['line_end']}**")
                    st.write(fact["statement"])
                    if "sample" in fact:
                        sample = fact["sample"]
                        st.code(
                            f"输入 {tuple(fact['sample_frame_shape'])} → 切片 {tuple(sample['shape'])}\n"
                            f"empty = {sample['empty']}\n"
                            f"归一化切片（起点、终点、步长）：{sample['normalized_slices']}",
                            language="text",
                        )
                    if "source_url" in fact:
                        st.markdown(f"[Keras 官方 fit 文档]({fact['source_url']})")
                    st.caption(fact["scope"])
    with coverage_tab:
        for name, state in report.checks.items():
            st.write(f"**{name}**：{state}")
        for limitation in report.limitations:
            st.write(f"• {limitation}")
    download1, download2, _ = st.columns([1, 1, 2])
    download1.download_button(
        "下载 Markdown",
        to_markdown(report, snapshot),
        file_name="review-report.md",
        mime="text/markdown",
        use_container_width=True,
    )
    download2.download_button(
        "下载 JSON",
        report.model_dump_json(indent=2),
        file_name="review-report.json",
        mime="application/json",
        use_container_width=True,
    )
    st.divider()
    st.subheader("04 / 继续追问")
    chat_enabled = (
        not changed and report.mode == "deepseek" and not is_demo and settings.model == report.model
    )
    if not chat_enabled:
        st.caption("使用 DeepSeek 完成当前代码的审查后，可结合报告继续追问。")
    for message in st.session_state.get("history", []):
        with st.chat_message(message["role"]):
            st.write(message["content"])
            if message.get("review_meta"):
                meta = message["review_meta"]
                usage = meta["metrics"]
                label = {
                    "completed": "校验完成",
                    "repaired": "修正后通过校验",
                    "blocked": "校验未通过",
                }[meta["status"]]
                st.caption(
                    f"{label} · {usage['elapsed_seconds']:.2f} 秒 · {usage['model_calls']} 次模型调用 · 输入/输出 token {usage['prompt_tokens']}/{usage['completion_tokens']}"
                )
                with st.expander("本轮校验记录"):
                    states = {"passed": "通过", "failed": "未通过", "skipped": "未进行"}
                    for attempt in meta.get("attempts", []):
                        st.caption(
                            f"第 {attempt['attempt']} 次候选 · "
                            f"本地校验：{states[attempt['local_status']]} · "
                            f"方案复核：{states[attempt['audit_status']]}"
                        )
                    for note in meta["checks"]:
                        st.write(note)
    with st.container():
        question = st.chat_input(
            "例如：第 1 个问题在什么情况下会发生？", disabled=not chat_enabled, max_chars=1500
        )
    if question:
        client = None
        try:
            client = DeepSeekClient(settings)
            with st.spinner("正在结合代码与报告回答…"):
                chat_agent = ReviewAgent(client, model_name=settings.model)
                answer = chat_agent.follow_up(
                    snapshot, report, question, st.session_state.get("history", [])
                )
            st.session_state.history = (
                st.session_state.get("history", [])
                + [
                    {"role": "user", "content": question},
                    {
                        "role": "assistant",
                        "content": answer,
                        "review_meta": chat_agent.last_followup.model_dump(exclude={"answer"})
                        if chat_agent.last_followup
                        else None,
                    },
                ]
            )[-8:]
            st.rerun()
        except (ValueError, ModelError) as error:
            st.error(str(error))
        finally:
            if client:
                client.close()
else:
    st.divider()
    st.caption("准备就绪 · 载入示例，点击「开始审查」即可体验完整流程。")
