"""Deterministic bounded selection for actual local-tool findings."""

from collections.abc import Iterable

from review_agent.models import Finding

PRIORITY = {"high": 0, "medium": 1, "low": 2}


def priority_key(finding: Finding) -> tuple:
    """Prefer severity before source order, with stable ties independent of traversal."""
    return (
        PRIORITY[finding.severity],
        finding.line_start,
        finding.line_end,
        finding.rule_id,
        finding.evidence,
        finding.title,
        finding.explanation,
        finding.suggestion,
    )


def select_findings(findings: Iterable[Finding], limit: int) -> tuple[list[Finding], dict]:
    """Count all input findings, remove equivalent diagnostics, then apply the cap.

    Different rules, evidence or diagnostic messages at the same line remain
    distinct: Ruff may report two undefined names in one source-line excerpt.
    This helper does not infer equivalence between paraphrased claims.
    Source input limits bound local collection; only retained findings are sent
    to the model or exported as tool output.
    """
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("finding limit must be a positive integer")
    ordered = sorted(findings, key=priority_key)
    unique, seen = [], set()
    for finding in ordered:
        key = (
            finding.rule_id,
            finding.line_start,
            finding.line_end,
            finding.evidence,
            finding.title,
            finding.explanation,
        )
        if key not in seen:
            seen.add(key)
            unique.append(finding)
    retained = unique[:limit]
    stats = {
        "total_count": len(ordered),
        "unique_count": len(unique),
        "retained_count": len(retained),
        "omitted_count": len(unique) - len(retained),
        "duplicate_count": len(ordered) - len(unique),
        "truncated": len(unique) > limit,
    }
    return retained, stats


def selection_notice(stats: dict, label: str) -> str:
    if not stats["truncated"]:
        return ""
    return (
        f"{label}结果已截断：共 {stats['total_count']} 条原始发现，"
        f"去重后 {stats['unique_count']} 条，按高→中→低优先级保留 "
        f"{stats['retained_count']} 条，省略 {stats['omitted_count']} 条。"
        "省略项未进入后续分析，不能据此认定没有其他问题。"
    )
