"""Ground citations in the source and separate review verdicts from model claims.

This module never executes submitted code. Exact quotes establish location, not
semantic correctness; model review and static rules are labelled accordingly.
"""

import ast
import io
import re
import tokenize
from bisect import bisect_right
from dataclasses import dataclass, field

from review_agent.claim_checks import (
    claim_issues,
    comparison_claim,
    comparison_speculation,
    requires_claim_review,
    unsupported_tool_claim,
)
from review_agent.cpp_tools import parse_cpp, walk
from review_agent.models import (
    AuditBatch,
    Finding,
    ReviewedFinding,
    ReviewInput,
    VerificationRecord,
)
from review_agent.semantics import conflicting_fact

PRIORITY = {"high": 0, "medium": 1, "low": 2}


@dataclass
class Candidate:
    finding_id: int
    finding: Finding
    static: bool = False
    grounding_error: str = ""
    quality_errors: list[str] = field(default_factory=list)
    needs_review: bool = False
    request: ReviewInput | None = None


def _line_offsets(code: str) -> list[int]:
    return [0] + [match.end() for match in re.finditer("\n", code)]


def _code_spans(request: ReviewInput) -> list[tuple[int, int]] | None:
    """Token spans exclude comments and standalone Python documentation strings."""
    code = request.code
    if request.language == "cpp":
        tree, _, _ = parse_cpp(code)
        if tree is None:
            return None
        # Tree-sitter measures UTF-8 bytes; Python string locations use characters.
        byte_to_char = []
        for index, char in enumerate(code):
            byte_to_char.extend([index] * len(char.encode("utf-8")))
        byte_to_char.append(len(code))
        return [
            (byte_to_char[node.start_byte], byte_to_char[node.end_byte])
            for node in walk(tree.root_node)
            if not node.children and node.type != "comment" and node.end_byte > node.start_byte
        ]
    offsets = _line_offsets(code)
    docstrings = set()
    try:
        for node in ast.walk(ast.parse(code)):
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                docstrings.add((node.lineno, node.col_offset))
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        pass
    ignored = {
        tokenize.COMMENT,
        tokenize.NL,
        tokenize.NEWLINE,
        tokenize.INDENT,
        tokenize.DEDENT,
        tokenize.ENDMARKER,
        tokenize.ENCODING,
    }
    spans = []
    try:
        for token in tokenize.generate_tokens(io.StringIO(code).readline):
            if token.type in ignored or token.start in docstrings or not token.string.strip():
                continue
            spans.append(
                (
                    offsets[token.start[0] - 1] + token.start[1],
                    offsets[token.end[0] - 1] + token.end[1],
                )
            )
    except (tokenize.TokenError, IndentationError, SyntaxError):
        # Earlier, successfully tokenized statements can still provide anchors.
        pass
    return spans


def static_findings(tool_results: dict) -> list[Finding]:
    result = tool_results.get("run_static_checks", {})
    return [
        Finding.model_validate(item)
        for item in (
            result.get("builtin_findings", []) + result.get("ruff", {}).get("findings", [])
        )
    ]


def ground_related(finding: Finding, request: ReviewInput) -> list[str]:
    """Locate every secondary quote independently; labels are not proof of semantics."""
    if not finding.related_evidence:
        return []
    code, offsets, spans = request.code, _line_offsets(request.code), _code_spans(request)
    errors = []
    for ref in finding.related_evidence:
        quote = ref.evidence.strip()
        ranges = sorted(
            {
                (bisect_right(offsets, match.start()), bisect_right(offsets, match.end() - 1))
                for match in re.finditer(re.escape(quote), code)
                if quote
                and spans is not None
                and any(a < match.end() and b > match.start() for a, b in spans)
            }
        )
        local = [(a, b) for a, b in ranges if ref.line_start <= a and b <= ref.line_end]
        chosen = local[0] if len(local) == 1 else ranges[0] if len(ranges) == 1 else None
        if chosen is None:
            errors.append(f"补充引用 {ref.role} 未原样匹配或不能唯一定位，不能充当另一端证据。")
        else:
            ref.line_start, ref.line_end = chosen
            ref.evidence = quote
    return errors


def reconcile_candidates(
    findings: list[Finding],
    request: ReviewInput,
    tool_results: dict,
    *,
    facts: list[dict] | None = None,
    limit: int = 30,
    model_draft: bool = True,
) -> tuple[list[Candidate], list[VerificationRecord], list[str]]:
    """Union real tool results with the draft *before* audit, then cap by priority.

    A model omitting a tool finding is not a rejection. Explicit audit rejections
    still take effect later and are never added back after finalization.
    """
    if limit < 1:
        raise ValueError("candidate limit must be positive")
    # Multiple diagnostics may share a rule and line; union all actual results
    # and deduplicate only after their identities and source anchors are known.
    missing = static_findings(tool_results)
    candidates, records = ground_candidates(findings + missing, request, tool_results, facts=facts)
    recovered = [item for item in candidates if item.static and item.finding_id > len(findings)]
    warnings = []
    if model_draft and recovered:
        records.extend(
            VerificationRecord(
                finding_id=item.finding_id,
                title=item.finding.title,
                status="recovered",
                reason="该发现来自本次实际静态工具，模型初审未纳入；程序已补回候选，仍接受后续上下文复核。",
            )
            for item in recovered
        )
        warnings.append(
            f"模型初审漏写的 {len(recovered)} 条实际工具发现已补回候选；最终去留见复核记录。"
        )
    # Preserve candidate IDs for traceability. For equal severity, actual tool
    # findings precede unsupported model candidates; do not compare prose length.
    candidates.sort(
        key=lambda item: (
            PRIORITY[item.finding.severity],
            not item.static,
            item.finding.line_start,
            item.finding.line_end,
            item.finding_id,
        )
    )
    if len(candidates) > limit:
        omitted = candidates[limit:]
        warnings.append(
            f"报告候选已截断：校验去重后共 {len(candidates)} 条，按高→中→低优先级保留 {limit} 条，"
            f"省略 {len(omitted)} 条（同级优先保留实际工具发现）；省略项见校验记录。"
        )
        records.extend(
            VerificationRecord(
                finding_id=item.finding_id,
                title=item.finding.title,
                status="truncated",
                reason=f"因 {limit} 条候选预算暂未纳入复核；优先级 {item.finding.severity}，位置 L{item.finding.line_start}。未判定为无问题。",
            )
            for item in omitted
        )
    return candidates[:limit], records, warnings


def ground_candidates(
    findings: list[Finding],
    request: ReviewInput,
    tool_results: dict,
    *,
    facts: list[dict] | None = None,
) -> tuple[list[Candidate], list[VerificationRecord]]:
    """Restore real tool findings; uniquely relocate exact quotes or quarantine them."""
    code, offsets = request.code, _line_offsets(request.code)
    spans = _code_spans(request)
    tool_findings = static_findings(tool_results)
    candidates, records, seen = [], [], set()

    def record(index, finding, status, reason):
        records.append(
            VerificationRecord(
                finding_id=index,
                title=finding.title,
                status=status,
                reason=reason,
            )
        )

    def matched_tool(finding, rule_id, *, grounded_span=False):
        matches = [
            item
            for item in tool_findings
            if item.rule_id == rule_id
            and (
                item.line_start == finding.line_start
                or (
                    grounded_span
                    and finding.line_start <= item.line_start <= finding.line_end
                    and item.evidence.strip() in finding.evidence
                )
            )
        ]
        if len(matches) > 1:
            matches = [
                item for item in matches if item.evidence.strip() == finding.evidence.strip()
            ]
        if len(matches) > 1:
            matches = [
                item
                for item in matches
                if item.title == finding.title and item.explanation == finding.explanation
            ]
        return matches[0] if len(matches) == 1 else None

    for index, original in enumerate(findings, 1):
        finding = original.model_copy(deep=True)
        is_static, error = False, ""
        if finding.rule_id:
            match = matched_tool(finding, finding.rule_id)
            if match:
                # Never attach a real rule label to a model's embellished claim.
                finding = match.model_copy(deep=True)
                is_static = True
            else:
                record(
                    index, finding, "rule_removed", "规则编号与本次工具位置不匹配，已移除工具背书。"
                )
                finding.rule_id = ""

        if is_static:
            # These rules explicitly lack type / input-origin context.
            if finding.rule_id in {"AST006", "CPP002"} and finding.severity == "high":
                finding.severity = "medium"
        else:
            quote = finding.evidence.strip()
            matches = [match for match in re.finditer(re.escape(quote), code)] if quote else []
            if spans is not None:
                matches = [
                    match
                    for match in matches
                    if any(start < match.end() and end > match.start() for start, end in spans)
                ]
            ranges = sorted(
                {
                    (bisect_right(offsets, match.start()), bisect_right(offsets, match.end() - 1))
                    for match in matches
                }
            )
            local = [
                span
                for span in ranges
                if (finding.line_start <= span[0] and span[1] <= finding.line_end)
            ]
            chosen = local[0] if len(local) == 1 else ranges[0] if len(ranges) == 1 else None
            if spans is None:
                error = "源码解析不可用，无法校验引用是否指向实际代码。"
            elif not ranges:
                error = (
                    "证据未原样匹配实际代码（可能是改写、拼接、注释或文档字符串），需补充准确引用。"
                )
            elif chosen is None:
                error = "证据在多处出现且当前范围无法唯一定位，需缩小到具体语句。"
            else:
                if chosen != (finding.line_start, finding.line_end):
                    record(
                        index,
                        finding,
                        "relocated",
                        (
                            f"按原样证据将 L{finding.line_start}–L{finding.line_end} "
                            f"校正为 L{chosen[0]}–L{chosen[1]}。"
                        ),
                    )
                finding.line_start, finding.line_end = chosen
                finding.evidence = quote
                # A uniquely relocated quote may now match the real tool anchor.
                restored = (
                    matched_tool(finding, original.rule_id, grounded_span=True)
                    if original.rule_id
                    else None
                )
                if restored:
                    finding = restored.model_copy(deep=True)
                    is_static = True
                    records = [
                        item
                        for item in records
                        if not (item.finding_id == index and item.status == "rule_removed")
                    ]

        if is_static and finding.rule_id in {"AST006", "CPP002"} and finding.severity == "high":
            finding.severity = "medium"
        conflict = conflicting_fact(finding, facts or []) if not error else ""
        if conflict:
            record(index, finding, "rejected", conflict)
            continue

        if finding.line_end > len(code.splitlines()):
            # Quarantined claims must not leave invalid offsets for UI/export slicing.
            record(index, finding, "rejected", "行号越界且无法由源码证据恢复位置。")
            continue
        key = (
            finding.line_start,
            finding.line_end,
            finding.rule_id,
            finding.evidence,
            finding.explanation,
            "" if is_static else finding.title.casefold(),
        )
        if key in seen:
            record(index, finding, "duplicate", "同一工具诊断或完全相同的模型候选已去重。")
            continue
        seen.add(key)
        quality_errors = ground_related(finding, request) + claim_issues(finding, static=is_static)
        candidates.append(
            Candidate(
                index,
                finding,
                is_static,
                error,
                quality_errors,
                bool(quality_errors) or (not is_static and requires_claim_review(finding)),
                request,
            )
        )
    return candidates, records


def validate_audit_ids(batch: AuditBatch, expected_ids: set[int]) -> None:
    ids = [decision.finding_id for decision in batch.decisions]
    if len(ids) != len(set(ids)) or set(ids) != expected_ids:
        raise ValueError("复核必须覆盖全部候选 ID，且不能重复或增加候选。")


def finalize_candidates(
    candidates: list[Candidate],
    batch: AuditBatch | None,
    records: list[VerificationRecord],
    *,
    reflection_status: str,
    facts: list[dict] | None = None,
) -> tuple[list[ReviewedFinding], list[ReviewedFinding], list[VerificationRecord]]:
    accepted, pending = [], []
    records = list(records)
    decisions = {item.finding_id: item for item in batch.decisions} if batch else {}
    for candidate in candidates:
        finding = candidate.finding.model_copy(deep=True)
        decision = decisions.get(candidate.finding_id)

        def record(status, reason):
            records.append(
                VerificationRecord(
                    finding_id=candidate.finding_id,
                    title=finding.title,
                    status=status,
                    reason=reason,
                )
            )

        if decision and not candidate.grounding_error:
            if decision.verdict == "rejected" or decision.impact_kind == "style":
                record("rejected", decision.reason)
                continue
            if not candidate.static:
                # Corrections also apply to uncertain/low-priority candidates; do not
                # leave their stale composite claims next to a corrected note.
                for name in (
                    "title",
                    "trigger",
                    "trigger_basis",
                    "evidence_role",
                    "related_evidence",
                ):
                    value = getattr(decision, name)
                    if value is not None:
                        setattr(finding, name, value)
                if decision.correction.strip():
                    finding.explanation = decision.correction
                if decision.suggestion.strip():
                    finding.suggestion = decision.suggestion

        # Validate roles/overlap after relocating secondary quotes, so neither
        # stale positions nor invented positions influence the final decision.
        quality_errors = ground_related(finding, candidate.request) if candidate.request else []
        quality_errors += claim_issues(finding, static=candidate.static)
        if decision and not candidate.static and unsupported_tool_claim(decision.reason):
            quality_errors.append(
                "复核说明仍含未核实的工具背书，不能通过 reason 恢复已移除的规则支持。"
            )
        if decision and comparison_claim(finding) and comparison_speculation(decision.reason):
            quality_errors.append(
                "复核说明仍在依据不足时断言图像极性或识别效果，不能当作已确认事实。"
            )
        if quality_errors:
            record(
                "withheld",
                "证据不足，未展示该候选（不等于证明不存在问题）："
                + "；".join(dict.fromkeys(quality_errors)),
            )
            continue

        conflict = (
            conflicting_fact(finding, facts or [], decision.reason if decision else "")
            if not candidate.grounding_error
            else ""
        )
        if conflict:
            record("rejected", conflict)
            continue
        verification, reason = "pending", candidate.grounding_error
        if not reason and decision:
            reason = decision.reason
            if decision.verdict == "supported" and decision.basis == "local_code":
                verification = "static" if candidate.static else "reviewed"
            elif decision.basis == "external_assumption":
                reason += " 结论依赖文件外的假设，需补充上下文。"
            if decision.impact_kind == "diagnostic":
                verification = "pending"
                reason += " 仅涉及诊断提示的改善，未说明功能错误，作为可选建议待确认。"
            if candidate.static:
                reason = "本次实际工具的规则、位置及原始诊断已匹配；模型复核未改变工具结论。"
        elif not reason and candidate.static:
            verification, reason = (
                "static",
                "规则编号与位置已匹配本次实际工具结果；保留工具原始结论。",
            )
        elif not reason:
            reason = (
                "模型复核失败；只有源码引用通过校验，语义结论尚未复核。"
                if reflection_status == "failed"
                else "低优先级候选未进行语义复核，作为可选建议待确认。"
                if finding.severity == "low" and reflection_status == "completed"
                else "未进行模型复核；只有源码引用通过校验，语义结论尚未复核。"
            )
        if not candidate.static and finding.trigger_basis == "external_condition":
            verification = "pending"
            reason += " 触发条件依赖外部数据/环境，未证实在当前调用中可达。"
        item = ReviewedFinding(
            **finding.model_dump(), verification=verification, review_note=reason
        )
        if decision and not candidate.grounding_error:
            if decision.severity:
                item.severity = max((item.severity, decision.severity), key=PRIORITY.get)
            if decision.impact_kind in {"diagnostic", "maintainability"}:
                item.severity = "low"
        if verification == "pending":
            if item.severity == "high":
                item.severity = "medium"
            if not item.verification_hint:
                item.verification_hint = (
                    "核对源码引用、触发条件及所需上下文后再确认；当前未执行测试。"
                )
            pending.append(item)
        else:
            accepted.append(item)
        record("pending" if verification == "pending" else "accepted", item.review_note)
    for items in (accepted, pending):
        items.sort(key=lambda item: (PRIORITY[item.severity], item.line_start))
    return accepted, pending, records


def report_summary(findings: list[ReviewedFinding], pending: list[ReviewedFinding]) -> str:
    """Rebuild from final decisions so rejected claims cannot survive in a summary."""
    static = sum(item.verification == "static" for item in findings)
    return (
        f"本次保留 {len(findings)} 条问题与建议（静态规则命中 {static} 条，"
        f"模型复核支持 {len(findings) - static} 条），另有 {len(pending)} 条待确认。"
        "源码引用及工具来源已检查；未执行被审查代码，结论仍需结合实际测试。"
    )
