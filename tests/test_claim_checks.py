"""Evidence regressions; scripted verdicts verify safeguards, not LLM accuracy."""

import json

import pytest
from test_agent import ScriptedClient, audit_response, draft_response, tool_response
from test_verification import candidate, decision, review_candidates

from review_agent.agent import ReviewAgent
from review_agent.claim_checks import claim_issues
from review_agent.followup import followup_context
from review_agent.models import EvidenceReference, ReviewedFinding, ReviewInput, ReviewReport
from review_agent.reporting import to_markdown
from review_agent.tools import ToolRegistry
from review_agent.verification import ground_candidates

LOAD = "(images, labels), _ = mnist.load_data()\n"
PAIR = "train = resize(images / 255.0)\ninfer = threshold(frame)\n"
FILTER = (
    "def first_positive(values):\n"
    "    selected = [x for x in values if x > 0]\n"
    "    return selected[0]\n"
)


def mixed(**updates):
    fields = dict(
        title="加载失败或筛选后空集未处理",
        explanation="数据下载失败、筛选后为空会使后续训练失败。",
        suggestion="提示下载错误，并检查筛选后空集。",
        severity="low",
        evidence=LOAD.strip(),
    )
    fields.update(updates)
    return candidate(**fields)


def comparison(**updates):
    fields = dict(
        title="训练和推理预处理不一致",
        explanation="训练使用灰度缩放，推理使用二值化，像素分布有差异；精度影响需实验。",
        suggestion="统一两端预处理并对比验证集表现。",
        severity="low",
        evidence="train = resize(images / 255.0)",
        evidence_role="training",
    )
    fields.update(updates)
    return candidate(**fields)


def inference_ref(**updates):
    fields = dict(role="inference", line_start=2, line_end=2, evidence="infer = threshold(frame)")
    fields.update(updates)
    return EvidenceReference(**fields)


def reachable_empty(**updates):
    fields = dict(
        title="正数筛选后可能为空，直接索引越界",
        explanation="当前函数允许负数列表，筛选为空后 selected[0] 抛出 IndexError。",
        evidence="selected = [x for x in values if x > 0]",
        evidence_role="condition",
        line_start=2,
        line_end=2,
        trigger="调用 first_positive([-1])，过滤后 selected=[]，执行 selected[0]。",
        trigger_basis="local_input",
        related_evidence=[
            EvidenceReference(
                role="consumer", line_start=3, line_end=3, evidence="return selected[0]"
            )
        ],
        suggestion="为空结果添加显式处理并测试负数列表。",
    )
    fields.update(updates)
    return candidate(**fields)


def test_mixed_low_priority_claim_requires_audit_and_cannot_pass_with_a_label():
    candidates, _ = ground_candidates([mixed()], ReviewInput(code=LOAD), {})
    assert candidates[0].needs_review and candidates[0].quality_errors
    accepted, pending, records = review_candidates(LOAD, [mixed()], [decision()])
    assert not accepted and not pending
    assert records[-1].status == "withheld"
    assert "不等于证明不存在" in records[-1].reason


def test_uncertain_verdict_narrows_all_prose_to_download_diagnostics():
    accepted, pending, _ = review_candidates(
        LOAD,
        [mixed()],
        [
            decision(
                verdict="uncertain",
                basis="external_assumption",
                impact_kind="diagnostic",
                reason="网络/缓存状态未提供，诊断提示是可选改善。",
                title="数据下载失败时可补充操作提示",
                correction="首次下载遇到网络错误时，当前调用会将异常交给上层。",
                suggestion="在数据加载边界捕获适当异常，附上重试提示。",
                trigger="首次下载且网络不可用；本次未复现。",
                trigger_basis="external_condition",
            )
        ],
    )
    assert not accepted and len(pending) == 1
    assert "空集" not in pending[0].model_dump_json()
    assert pending[0].trigger_basis == "external_condition"


def test_narrowing_only_title_and_explanation_does_not_leave_empty_claim_in_suggestion():
    accepted, pending, records = review_candidates(
        LOAD,
        [mixed()],
        [decision(title="下载错误提示", correction="加载失败时缺少操作提示。", reason="待确认。")],
    )
    assert not accepted and not pending and records[-1].status == "withheld"


def test_legitimate_empty_input_issue_with_trigger_and_two_quotes_is_preserved():
    accepted, pending, _ = review_candidates(FILTER, [reachable_empty()], [decision()])
    assert len(accepted) == 1 and not pending
    assert accepted[0].trigger_basis == "local_input"


@pytest.mark.parametrize("trigger", ["", "数据异常", "当筛选后为空时"])
def test_vague_empty_trigger_cannot_replace_a_reachable_example(trigger):
    accepted, pending, records = review_candidates(
        FILTER, [reachable_empty(trigger=trigger)], [decision()]
    )
    assert not accepted and not pending and records[-1].status == "withheld"


def test_external_trigger_cannot_be_promoted_by_local_code_audit_label():
    accepted, pending, _ = review_candidates(
        FILTER,
        [
            reachable_empty(
                trigger_basis="external_condition", trigger="替换外部数据源为无正数的数据。"
            )
        ],
        [decision()],
    )
    assert not accepted and pending[0].verification == "pending"


def test_one_sided_preprocessing_comparison_is_withheld_even_after_support_verdict():
    accepted, pending, records = review_candidates(PAIR, [comparison()], [decision()])
    assert not accepted and not pending
    assert records[-1].status == "withheld"


def test_list_comprehension_empty_claim_also_requires_condition_and_consumer():
    item = reachable_empty(
        title="空列表或无正数时发生索引错误",
        explanation="列表推导得到空列表，随后 selected[0] 越界。",
        evidence_role="related",
        related_evidence=[],
    )
    accepted, pending, records = review_candidates(FILTER, [item], [decision()])
    assert not accepted and not pending and records[-1].status == "withheld"


@pytest.mark.parametrize("in_reason", [False, True])
def test_two_citations_cannot_prove_unknown_image_polarity(in_reason):
    text = "训练与推理预处理不同，推理二值化得到的是白底黑字，前景/背景极性相反。"
    item = comparison(related_evidence=[inference_ref()])
    audit = decision(reason=text if in_reason else "引用完整。")
    if not in_reason:
        item.explanation = text
    accepted, pending, records = review_candidates(PAIR, [item], [audit])
    assert not accepted and not pending and records[-1].status == "withheld"


def test_explaining_polarity_uncertainty_is_allowed():
    item = comparison(
        related_evidence=[inference_ref()],
        explanation="训练与推理预处理存在操作差异。不能据此断言极性相反，需用实际图像验证。",
    )
    assert not claim_issues(item)


def test_mnist_training_description_is_not_mistaken_for_inference_polarity_claim():
    item = comparison(
        related_evidence=[inference_ref()],
        explanation="训练输入是 MNIST 灰度图（黑底白字）。推理使用阈值二值化；预处理操作不同，影响需实验。",
    )
    assert not claim_issues(item)


def test_comparison_repair_relocates_second_quote_before_checking_overlap_and_exports_both():
    accepted, pending, records = review_candidates(
        PAIR,
        [comparison()],
        [
            decision(
                impact_kind="maintainability",
                reason="两端操作有差异，未测量识别率。",
                related_evidence=[inference_ref(line_start=1, line_end=1)],
            )
        ],
    )
    assert len(accepted) == 1 and not pending
    assert accepted[0].related_evidence[0].line_start == 2
    request = ReviewInput(code=PAIR)
    report = ReviewAgent().review(request)
    report.findings, report.pending_findings, report.verification_records = accepted, [], records
    markdown = to_markdown(report, request)
    assert "训练端 · L1–L1" in markdown and "推理端 · L2–L2" in markdown
    assert "infer = threshold(frame)" in markdown


@pytest.mark.parametrize(
    "code,ref",
    [
        (PAIR, inference_ref(evidence="infer = missing(frame)")),
        (PAIR, inference_ref(evidence="train = resize(images / 255.0)")),
        ("train = resize(images / 255.0)\n# infer = threshold(frame)\n", inference_ref()),
        (PAIR + "infer = threshold(frame)\n", inference_ref(line_start=1, line_end=1)),
    ],
)
def test_fabricated_overlapping_comment_or_ambiguous_secondary_quote_is_withheld(code, ref):
    accepted, pending, records = review_candidates(
        code, [comparison(related_evidence=[ref])], [decision()]
    )
    assert not accepted and not pending and records[-1].status == "withheld"


@pytest.mark.parametrize("field", ["title", "explanation", "suggestion", "trigger"])
def test_removed_rule_cannot_survive_as_free_text_in_any_finding_field(field):
    item = candidate(
        evidence="conf = 1", rule_id="RUFF:F841", **{field: "Ruff F841 报在第 145 行。"}
    )
    accepted, pending, records = review_candidates("conf = 1\n", [item], [decision()])
    assert not accepted and not pending
    assert {record.status for record in records} >= {"rule_removed", "withheld"}


def test_audit_reason_cannot_restore_removed_tool_endorsement():
    accepted, pending, records = review_candidates(
        "conf = 1\n",
        [candidate(evidence="conf = 1")],
        [decision(reason="Ruff 检测到未使用变量。")],
    )
    assert not accepted and not pending and records[-1].status == "withheld"


def test_real_ruff_conf_result_restores_exact_line_and_canonical_prose():
    # Module assignments do not trigger F841; use a function and inspect real Ruff output.
    request = ReviewInput(code="def classify(score):\n    conf = score\n    return 1\n")
    registry = ToolRegistry(request)
    registry.execute("run_static_checks", "{}")
    ruff = registry.results["run_static_checks"]["ruff"]["findings"]
    actual = next(item for item in ruff if item["rule_id"] == "RUFF:F841")
    assert actual["line_start"] == 2
    accepted, pending, _ = review_candidates(
        request.code,
        [
            candidate(
                line_start=1,
                line_end=1,
                evidence="conf = score",
                rule_id="RUFF:F841",
                explanation="Ruff F841 报在 145 行，所以这是功能错误。",
            )
        ],
        tool_results=registry.results,
    )
    assert not pending and accepted[0].verification == "static"
    assert accepted[0].line_start == 2
    assert accepted[0].explanation == actual["explanation"]
    assert "145" not in accepted[0].model_dump_json()


def test_risky_low_candidate_shares_existing_audit_round_with_no_extra_call():
    client = ScriptedClient(
        [
            tool_response(),
            draft_response([comparison()]),
            audit_response(
                related_evidence=[inference_ref()], reason="两端证据完整，精度影响未测。"
            ),
        ]
    )
    code = (
        "def compare(resize, images, threshold, frame):\n"
        "    train = resize(images / 255.0)\n"
        "    infer = threshold(frame)\n"
        "    return train, infer\n"
    )
    report = ReviewAgent(client).review(ReviewInput(code=code))
    assert report.metrics.model_calls == 3 and len(report.findings) == 1
    audit_input = json.loads(client.messages[-1][1]["content"])
    assert audit_input["candidates"][0]["quality_errors"]
    assert audit_input["candidates"][0]["actual_static"] is False


def test_old_report_followup_quarantines_bad_claim_without_mutation_or_false_rejection():
    request = ReviewInput(code=LOAD)
    report = ReviewAgent().review(request)
    report.schema_version = 4
    report.pending_findings = [ReviewedFinding(**mixed().model_dump())]
    before = report.model_dump_json()
    context = followup_context(request, report, [])
    assert not context["report"]["pending_findings"]
    assert context["withheld_claims"][0]["reference"] == "pending:1"
    assert not context["rejected_claims"]
    assert report.model_dump_json() == before


def test_old_json_loads_with_safe_default_evidence_fields():
    report = ReviewAgent().review(ReviewInput(code="x = 1\n"))
    data = report.model_dump()
    data["schema_version"] = 4
    item = comparison().model_dump()
    for name in ("evidence_role", "related_evidence", "trigger", "trigger_basis"):
        item.pop(name)
    data["pending_findings"] = [item]
    restored = ReviewReport.model_validate(data)
    assert claim_issues(restored.pending_findings[0])
