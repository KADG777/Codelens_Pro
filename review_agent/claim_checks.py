"""Bounded checks for unsupported composite claims and free-text tool attribution.

These checks request stronger evidence, not prove general program reachability.
They do not execute code or assume the contents of a downloaded dataset.
"""

import re

from review_agent.models import Finding


def unsupported_tool_claim(text: str) -> bool:
    """Tool attribution belongs to application-verified metadata, not model prose."""
    return bool(
        re.search(r"\b(?:RUFF\s*:\s*)?[FE]\d{3}\b|\b(?:AST|CPP)\d{3}\b", text, re.I)
        or re.search(
            r"(?:Ruff|Tree.?sitter|静态(?:工具|检查)|解析工具).{0,35}(?:已|报在|报出|报告|命中|发现|检测到|指出|支持|证实|即可发现|found|reported|confirmed)",
            text,
            re.I,
        )
        or re.search(
            r"(?:根据|依据|通过).{0,8}(?:Ruff|静态检查).{0,10}(?:可知|确定|证明)", text, re.I
        )
    )


def comparison_claim(finding: Finding) -> bool:
    text = finding.title + "。" + finding.explanation
    return bool(
        re.search(r"训练|training", text, re.I)
        and re.search(r"推理|预测|inference|prediction", text, re.I)
        and re.search(r"预处理|缩放|二值|膨胀|归一化|preprocess|normaliz", text, re.I)
    )


def empty_filter_claim(finding: Finding) -> bool:
    text = "。".join((finding.title, finding.explanation, finding.suggestion))
    clauses = re.split(r"[。；;\n]", text)
    return any(
        (
            re.search(r"筛选|过滤|列表推导|filter|selection|comprehension", clause, re.I)
            or re.search(r"\[.+\bfor\b.+\bif\b", finding.evidence)
        )
        and re.search(
            r"空集|为空|后空|空数组|空列表|无样本|样本数.{0,5}(?:0|零)|empty", clause, re.I
        )
        and not re.search(r"不会为空|不为空|非空|不能.{0,8}(?:断定|认定)|没有证据|未证明", clause)
        for clause in clauses
    )


def comparison_speculation(text: str) -> bool:
    """Narrow guard: operation names alone do not establish image polarity/accuracy."""
    for clause in re.split(r"[。；;\n]", text):
        if re.search(r"取决于|无法|不能|未验证|尚未|不一定|不代表|需.{0,8}验证", clause):
            continue
        if re.search(
            r"(?:得到|变成|输出).{0,12}(?:白底黑字|黑底白字)"
            r"|(?:前景|背景|极性|像素分布).{0,10}(?:相反|颠倒)"
            r"|(?:预测|识别).{0,10}系统性错误"
            r"|(?:导致|造成).{0,8}(?:精度|准确率|识别率).{0,6}(?:下降|降低)",
            clause,
        ):
            return True
    return False


def claim_issues(finding: Finding, *, static: bool = False) -> list[str]:
    if static:
        return []  # Canonical content was restored from an actual tool finding.
    issues = []
    prose = "\n".join((finding.title, finding.explanation, finding.suggestion, finding.trigger))
    if unsupported_tool_claim(prose):
        issues.append(
            "正文含未经本次规则与位置校验的工具背书；去掉工具命中说法，不能借用其他行的规则。"
        )
    refs = [(finding.evidence_role, finding.line_start, finding.line_end)] + [
        (ref.role, ref.line_start, ref.line_end) for ref in finding.related_evidence
    ]
    if comparison_claim(finding):
        if comparison_speculation(prose):
            issues.append(
                "预处理操作差异不能单独证明图像极性相反或识别率受损；收窄为可观察操作，标明输入图像/实验缺口。"
            )
        training = [(a, b) for role, a, b in refs if role == "training"]
        inference = [(a, b) for role, a, b in refs if role == "inference"]
        if not any(b < c or d < a for a, b in training for c, d in inference):
            issues.append(
                "训练/推理预处理比较必须提供两端独立、不重叠的原样源码引用，并标注 training/inference。"
            )
    if empty_filter_claim(finding):
        roles = {role for role, _, _ in refs}
        if (
            not finding.trigger.strip()
            or finding.trigger_basis == "unspecified"
            or re.fullmatch(
                r"(?:当|如果|若)?(?:数据异常|数据为空|筛选后为空|没有样本|输入异常)[时则。\s]*",
                finding.trigger.strip(),
            )
        ):
            issues.append("筛选为空缺少具体可达输入/路径及其来源；不能只写数据异常或可能为空。")
        if not {"condition", "consumer"}.issubset(roles):
            issues.append(
                "筛选为空必须引用过滤条件及使用筛选结果的故障语句；仅引用 load_data 不足。"
            )
        if re.search(r"(?:load_data|加载|下载).{0,30}(?:失败|异常)", prose, re.I):
            issues.append("加载失败与筛选为空是不同机制，不能混为一条；收窄为有证据的一项或否决。")
    return issues


def requires_claim_review(finding: Finding) -> bool:
    return comparison_claim(finding) or empty_filter_claim(finding)
