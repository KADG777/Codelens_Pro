"""Portable Markdown export from validated reports."""

from review_agent.languages import LANGUAGE_LABELS
from review_agent.models import ReviewInput, ReviewReport

SEVERITY = {"high": "高", "medium": "中", "low": "低"}
VERIFICATION = {"static": "静态规则命中", "reviewed": "模型复核支持", "pending": "待确认"}
EVIDENCE_ROLES = {
    "related": "相关代码",
    "training": "训练端",
    "inference": "推理端",
    "condition": "过滤/触发条件",
    "consumer": "结果使用点",
    "source": "数据来源",
}
TRIGGER_BASES = {
    "unspecified": "尚未说明来源",
    "local_input": "当前接口输入/源码路径（未运行）",
    "external_condition": "外部数据或环境假设（未证实可达）",
}


def code_block(code: str, language: str) -> str:
    # Submitted comments can contain Markdown fences. Keep exported quotes intact.
    import re

    longest = max((len(match[0]) for match in re.finditer(r"`+", code)), default=0)
    fence = "`" * max(4, longest + 1)
    return f"{fence}{language}\n{code}\n{fence}"


def to_markdown(report: ReviewReport, request: ReviewInput) -> str:
    parts = [
        f"# 代码审查报告：{report.filename}",
        f"语言：{LANGUAGE_LABELS[report.language]}",
        f"模式：{'离线规则演示' if report.mode == 'demo' else 'DeepSeek'} | 模型：{report.model}",
        f"策略：{report.strategy} | 快照：{report.source_hash} | 时间：{report.created_at}",
        f"复核状态：{report.reflection_status}",
        f"## 概览\n\n{report.summary}",
    ]
    lines = request.code.splitlines()
    parts.append("## 问题与建议")
    for index, finding in enumerate(report.findings + report.pending_findings, 1):
        snippet = "\n".join(lines[finding.line_start - 1 : finding.line_end])
        parts.extend(
            [
                f"### {index}. [{SEVERITY[finding.severity]} · {VERIFICATION[finding.verification]}] {finding.title}",
                f"位置：L{finding.line_start}–L{finding.line_end} | 类型：{finding.category}",
                f"依据状态：{VERIFICATION[finding.verification]} | 规则：{finding.rule_id or '无'}",
                code_block(snippet, report.language),
                f"**{'候选说法（尚未确认）' if finding.verification == 'pending' else '问题与触发条件'}**：{finding.explanation}",
                f"**引用证据 · {EVIDENCE_ROLES[finding.evidence_role]} · L{finding.line_start}–L{finding.line_end}**：\n\n"
                + code_block(finding.evidence, report.language),
            ]
        )
        for ref in finding.related_evidence:
            parts.append(
                f"**补充证据 · {EVIDENCE_ROLES[ref.role]} · L{ref.line_start}–L{ref.line_end}**：\n\n"
                + code_block("\n".join(lines[ref.line_start - 1 : ref.line_end]), report.language)
            )
        if finding.trigger:
            parts.append(
                f"**具体触发条件**：{finding.trigger}\n\n条件来源：{TRIGGER_BASES[finding.trigger_basis]}。"
            )
        parts.extend(
            [
                f"**复核说明**：{finding.review_note}",
                f"**{'候选建议（需验证）' if finding.verification == 'pending' else '修改建议'}**：{finding.suggestion}",
            ]
        )
        if finding.verification_hint:
            parts.append(f"**建议验证（未执行）**：{finding.verification_hint}")
    if not report.findings:
        parts.append("本次没有通过校验/复核的问题；请查看待确认项，不代表代码已被证明正确。")
    if report.verification_records:
        parts.append("## 校验与复核记录")
        parts.extend(
            f"- 候选 {item.finding_id} · {item.title} · {item.status}：{item.reason}"
            for item in report.verification_records
        )
    if report.semantic_facts:
        parts.append("## 切片计算与 API 依据")
        for fact in report.semantic_facts:
            detail = (
                f"{fact['id']} · L{fact['line_start']}–L{fact['line_end']}：{fact['statement']}"
            )
            if "sample" in fact:
                detail += f" 算例 {fact['sample_frame_shape']} → {fact['sample']['shape']}，empty={fact['sample']['empty']}。"
            if "source_url" in fact:
                detail += f" [Keras 官方 fit 文档]({fact['source_url']})"
            parts.append(detail + "\n\n适用范围：" + fact["scope"])
    if report.calculations:
        import json

        parts.append("## 工具计算记录\n\n参数由模型选择，计算结果不自动证明源码行为。")
        parts.append(
            code_block(json.dumps(report.calculations, ensure_ascii=False, indent=2), "json")
        )
    parts.append("## 覆盖与限制\n\n" + "\n".join(f"- {item}" for item in report.limitations))
    parts.append(
        "## 执行记录\n\n" + "\n".join(f"- {item.stage}：{item.detail}" for item in report.events)
    )
    parts.append(
        f"耗时 {report.metrics.elapsed_seconds} 秒；模型调用 {report.metrics.model_calls} 次；"
        f"工具调用 {report.metrics.tool_calls} 次；"
        f"输入/输出 token {report.metrics.prompt_tokens}/{report.metrics.completion_tokens}。"
    )
    if report.metrics.stages:
        rows = [
            "| 阶段 | 秒 | 模型调用 | HTTP 尝试 | 输入 token | 输出 token | 状态 |",
            "|---|---:|---:|---:|---:|---:|---|",
        ]
        rows += [
            f"| {item.stage} | {item.elapsed_seconds:.3f} | {item.model_calls} | {item.http_attempts} | {item.prompt_tokens} | {item.completion_tokens} | {item.status} |"
            for item in report.metrics.stages
        ]
        parts.append("## 各阶段开销\n\n" + "\n".join(rows))
    return "\n\n".join(parts) + "\n"
