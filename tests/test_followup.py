import json

import pytest
from test_agent import ScriptedClient, followup_response
from test_semantics import KERAS, ROI

from review_agent.agent import ReviewAgent
from review_agent.followup import (
    FollowupDraft,
    PatchProposal,
    check_draft,
    followup_context,
    numeric_fact_conflicts,
    preprocessing_conflicts,
    wants_patch,
)
from review_agent.llm import ModelError
from review_agent.models import ReviewedFinding, ReviewInput
from review_agent.semantics import source_facts, text_conflicts


def base(code=ROI, filename="a.py"):
    request = ReviewInput(code=code, filename=filename)
    return request, ReviewAgent().review(request)


def test_old_report_contradiction_is_labelled_rejected_without_mutating_saved_report():
    request, report = base()
    report.pending_findings = [
        ReviewedFinding(
            title="低分辨率 ROI 为空而崩溃",
            severity="medium",
            category="Bug",
            line_start=3,
            line_end=6,
            evidence="roi = frame[y1:y2, x1:x2]",
            explanation="160×120 下 ROI 会得到空数组。",
            suggestion="修复",
            verification="pending",
        )
    ]
    context = followup_context(request, report, [])
    assert not context["report"]["pending_findings"]
    assert context["rejected_claims"][0]["reference"] == "pending:1"
    assert "已否决" in context["reference_labels"]["pending:1"]
    assert report.pending_findings  # Migration is a view, not rewriting history.


@pytest.mark.parametrize(
    "bad",
    [
        "ROI 在低分辨率下会导致崩溃。",
        "160×120 时 ROI 得到 0 行，必须修复。",
        "ROI 为空数组。后来补充：ROI 非空。",
    ],
)
def test_bad_roi_answer_is_repaired_before_display(bad):
    request, report = base()
    client = ScriptedClient(
        [
            followup_response(bad),
            followup_response("ROI 的 160×120 算例得到 (40,20,3)，非空。", references=["fact_1"]),
        ]
    )
    agent = ReviewAgent(client)
    answer = agent.follow_up(request, report, "ROI 会崩溃吗？")
    assert agent.last_followup.status == "repaired"
    assert bad not in answer
    assert "(40,20,3)" in answer
    assert agent.last_followup.metrics.model_calls == 2
    assert "校验错误" in client.messages[1][-1]["content"]


def test_repeated_contradictions_fail_closed_with_calculation_fallback():
    request, report = base()
    client = ScriptedClient([followup_response("ROI 为空数组。")] * 2)
    agent = ReviewAgent(client)
    answer = agent.follow_up(request, report, "解释问题")
    assert agent.last_followup.status == "blocked"
    assert "本轮回答未通过校验" in answer and "基本切片非空" in answer
    assert "ROI 为空数组。" not in answer


def test_service_failure_does_not_expose_draft_or_unbounded_retry():
    request, report = base()
    agent = ReviewAgent(ScriptedClient([ModelError("down")]))
    assert "未通过校验" in agent.follow_up(request, report, "解释")
    assert agent.last_followup.metrics.model_calls == 1
    assert agent.last_followup.metrics.stages[0].status == "failed"


@pytest.mark.parametrize(
    "text",
    [
        "ROI 非空，但位置偏离预期中心。",
        "不能认定低分辨率下 ROI 会崩溃。",
        "ROI 的 empty=False，形状为 (40,20,3)。",
        "如果高度为零，ROI 为空数组。",
        "Keras validation_split 不是随机切分；同源不等于泄漏。",
    ],
)
def test_correct_negations_and_scope_are_not_blocked(text):
    assert not text_conflicts(text, source_facts(ROI + KERAS, "python"))


def test_false_keras_claim_is_blocked():
    assert text_conflicts("validation_split 随机切分验证集。", source_facts(KERAS, "python"))


def test_correctly_grounded_patch_is_shown_and_source_is_unchanged():
    code = "def average(values):\n    return sum(values) / len(values)\n"
    request, report = base(code)
    patch = dict(
        line_start=1,
        line_end=2,
        original=code.rstrip(),
        replacement="def average(values):\n    if not values:\n        raise ValueError('empty input')\n    return sum(values) / len(values)",
        rationale="为空输入添加明确处理。",
    )
    client = ScriptedClient(
        [
            followup_response("下面给出局部修改。", patches=[patch]),
            {"content": '{"verdict":"supported","reason":"保留接口，空列表抛出明确异常。"}'},
        ]
    )
    agent = ReviewAgent(client)
    answer = agent.follow_up(request, report, "优化这段代码")
    assert agent.last_followup.status == "completed" and agent.last_followup.patch_count == 1
    assert "替换原文件 L1–L2" in answer and "raise ValueError" in answer
    assert request.code == code
    assert "未运行测试" in answer


@pytest.mark.parametrize("replacement", ["def f(:", "value = unknown_helper()"])
def test_invalid_python_patch_is_not_shown(replacement):
    request, report = base("value = 1\n")
    draft = FollowupDraft(
        answer="建议修改。",
        patches=[
            PatchProposal(
                line_start=1,
                line_end=1,
                original="value = 1",
                replacement=replacement,
                rationale="修改",
            )
        ],
        validation_steps=["检查边界输入"],
    )
    assert check_draft(draft, request, followup_context(request, report, []), True)


def test_wrong_original_or_overlapping_patch_is_rejected():
    request, report = base("value = 1\n")
    context = followup_context(request, report, [])
    draft = FollowupDraft(
        answer="建议修改",
        validation_steps=["检查"],
        patches=[
            PatchProposal(
                line_start=1,
                line_end=1,
                original="value = 2",
                replacement="value = 3",
                rationale="修改",
            )
        ],
    )
    assert any("原样匹配" in e for e in check_draft(draft, request, context, True))
    draft.patches[0].original = "value = 1"
    draft.patches.append(draft.patches[0])
    assert any("重叠" in e for e in check_draft(draft, request, context, True))


def test_optimization_without_patch_or_invented_reference_cannot_pass():
    request, report = base()
    context = followup_context(request, report, [])
    draft = FollowupDraft(
        answer="如果希望我可以写代码。", references=["finding:99"], validation_steps=["测试"]
    )
    errors = check_draft(draft, request, context, True)
    assert any("局部替换" in e for e in errors) and any("不存在" in e for e in errors)


def test_cpp_patch_checks_syntax_without_compiling():
    request, report = base("int f() { return 1; }\n", "a.cpp")
    draft = FollowupDraft(
        answer="改为返回 2。",
        patches=[
            PatchProposal(
                line_start=1,
                line_end=1,
                original=request.code.rstrip(),
                replacement="int f() { return 2; }",
                rationale="修改返回值",
            )
        ],
        validation_steps=["编译后验证返回 2；未执行。"],
    )
    assert not check_draft(draft, request, followup_context(request, report, []), True)
    draft.patches[0].replacement = "int f( { return 2;"
    assert check_draft(draft, request, followup_context(request, report, []), True)


def test_history_is_bounded_data_and_metrics_reconcile():
    request, report = base()
    client = ScriptedClient([followup_response("ROI 非空。")])
    agent = ReviewAgent(client)
    agent.follow_up(request, report, "解释", [{"role": "assistant", "content": "ROI 为空！"}] * 12)
    context = json.loads(client.messages[0][1]["content"])
    assert len(context["history"]) == 8
    assert len(client.messages[0]) == 3
    assert agent.last_followup.metrics.model_calls == sum(
        s.model_calls for s in agent.last_followup.metrics.stages
    )


def test_explicit_no_code_request_does_not_require_patch():
    assert wants_patch("你应该把代码优化一下提高识别率")
    assert not wants_patch("只解释优化方向，不要代码")


def test_validation_steps_cannot_claim_execution():
    request, report = base("value = 1\n")
    context = followup_context(request, report, [])
    draft = FollowupDraft(answer="解释。", validation_steps=["已经测试通过。"])
    assert any("不得声称" in e for e in check_draft(draft, request, context, False))
    draft.validation_steps = ["尚未运行，请用两个边界输入验证。"]
    assert not check_draft(draft, request, context, False)


def test_nonempty_but_wrong_roi_dimensions_are_rejected():
    facts = source_facts(ROI, "python")
    assert numeric_fact_conflicts("160×120 时 ROI 形状为 (120,160,3)，非空。", facts)
    assert numeric_fact_conflicts("按 fact_1，负起点按 0 处理。", facts)
    assert not numeric_fact_conflicts("160×120 时 ROI 形状为 (40,20,3)，非空。", facts)
    assert not numeric_fact_conflicts("按 fact_1，不能把负起点直接置 0。", facts)


@pytest.mark.parametrize("text", ["不能保证一定提高识别率。", "无法保证识别率一定提高。"])
def test_cautious_accuracy_statement_is_not_a_guarantee(text):
    request, report = base("value = 1\n")
    draft = FollowupDraft(answer=text, validation_steps=["使用同一测试集对比。"])
    assert not check_draft(draft, request, followup_context(request, report, []), False)


def test_keras_comparison_or_negation_is_not_a_leakage_claim():
    facts = source_facts(KERAS, "python")
    assert not text_conflicts("对照同源但不重叠的划分与真实泄漏样例。", facts)
    assert not text_conflicts("同源本身不证明数据泄漏。", facts)
    assert text_conflicts("验证集同源所以存在数据泄漏。", facts)
    assert not text_conflicts("若想随机切分验证集，应显式构造 validation_data。", facts)
    assert not text_conflicts("ROI 不是空数组。", source_facts(ROI, "python"))


@pytest.mark.parametrize("final_verdict", ["supported", "revise"])
def test_patch_semantics_audit_repairs_once_or_blocks(final_verdict):
    request, report = base("value = 1\n")
    proposal = dict(
        line_start=1,
        line_end=1,
        original="value = 1",
        replacement="value = 2",
        rationale="改为 2。",
    )
    candidate = followup_response("应用补丁后返回 2。", patches=[proposal])
    client = ScriptedClient(
        [
            candidate,
            {"content": '{"verdict":"revise","reason":"补充符合要求的修改说明。"}'},
            candidate,
            {"content": json.dumps({"verdict": final_verdict, "reason": "已核对修改目的。"})},
        ]
    )
    agent = ReviewAgent(client)
    answer = agent.follow_up(request, report, "请优化代码")
    assert agent.last_followup.metrics.model_calls == 4
    assert agent.last_followup.status == ("repaired" if final_verdict == "supported" else "blocked")
    assert ("替换原文件" in answer) == (final_verdict == "supported")
    assert len([s for s in agent.last_followup.metrics.stages if s.stage == "追问方案复核"]) == 2


def test_unifying_preprocessing_checks_both_paths_through_helpers():
    request = ReviewInput(
        code="import cv2\ndef normalize(x):\n    return cv2.resize(x,(20,20))\ndef training_pixels(x):\n    return normalize(x)\ndef camera_pixels(x):\n    return normalize(cv2.dilate(x, None))\n"
    )
    draft = FollowupDraft(answer="建议", validation_steps=["对照"], patches=[])
    assert preprocessing_conflicts(request, draft, "统一训练和推理预处理")
    assert not preprocessing_conflicts(request, draft, "解释为什么处理方式不同")
    request.code = request.code.replace(
        "return normalize(cv2.dilate(x, None))", "return normalize(x)"
    )
    assert not preprocessing_conflicts(request, draft, "统一训练和推理预处理")


@pytest.mark.parametrize(
    "code,question,expected",
    [
        (ROI, "160×120 时 ROI 为空吗？", "40, 20, 3"),
        (KERAS, "validation_split 随机切分吗，同源能证明泄漏吗？", "先取末尾"),
    ],
)
def test_known_fact_questions_do_not_rely_on_model_arithmetic(code, question, expected):
    request, report = base(code)
    client = ScriptedClient([])
    agent = ReviewAgent(client)
    answer = agent.follow_up(request, report, question)
    assert expected in answer
    assert agent.last_followup.metrics.model_calls == 0 and not client.messages
