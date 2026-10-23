import json
from itertools import product
from pathlib import Path

import numpy as np
import pytest
from test_agent import ScriptedClient, audit_response, draft_response, finding, tool_response

from review_agent.agent import ReviewAgent
from review_agent.models import AuditBatch, ReviewInput
from review_agent.semantics import (
    AxisSlice,
    BroadcastArguments,
    SliceArguments,
    SplitArguments,
    calculate_slice,
    check_broadcast,
    conflicting_fact,
    keras_split,
    source_facts,
)
from review_agent.tools import ToolRegistry
from review_agent.verification import finalize_candidates, ground_candidates

FIXTURE = (Path(__file__).resolve().parents[1] / "evals/performance_fixture.py").read_text(
    encoding="utf-8"
)
ROI = """def crop(frame):
    height, width = frame.shape[:2]
    box_size = 200
    x1, y1 = (width - box_size) // 2, (height - box_size) // 2
    x2, y2 = x1 + box_size, y1 + box_size
    roi = frame[y1:y2, x1:x2]
    return roi
"""
KERAS = "from keras import Sequential\nmodel = Sequential()\nmodel.fit(x, y, validation_split=0.2, shuffle=True)\n"


def claim(code, text, evidence):
    item = finding().model_copy(
        update={
            "title": text,
            "explanation": text,
            "evidence": evidence,
            "line_start": 1,
            "line_end": len(code.splitlines()),
        }
    )
    return item


def test_roi_160_by_120_is_nonempty_using_independent_numpy_oracle():
    frame = np.zeros((120, 160, 3))
    actual = frame[-40:160, -20:180]
    fact = source_facts(ROI, "python")[0]
    assert list(actual.shape) == fact["sample"]["shape"] == [40, 20, 3]
    assert actual.size > 0 and fact["sample"]["empty"] is False
    assert fact["sample"]["normalized_slices"] == [[80, 120, 1], [140, 160, 1]]


def test_general_slice_calculation_matches_numpy_for_zero_negative_and_clipped_axes():
    for size, start, stop, step in product(
        (0, 1, 7), (None, -10, 0, 3, 10), (None, -8, 0, 4, 12), (-2, -1, 1, 2)
    ):
        result = calculate_slice(
            SliceArguments(shape=[size, 3], slices=[AxisSlice(start=start, stop=stop, step=step)])
        )
        actual = np.zeros((size, 3))[slice(start, stop, step)]
        assert result["shape"] == list(actual.shape)
        assert result["empty"] == (actual.size == 0)


def test_center_formula_is_nonempty_for_positive_dimensions_even_when_box_is_larger():
    for height, width, box in product((1, 2, 19, 120), (1, 3, 27, 160), (1, 2, 199, 200, 1000)):
        y, x = (height - box) // 2, (width - box) // 2
        actual = np.zeros((height, width, 3))[y : y + box, x : x + box]
        assert actual.size > 0


@pytest.mark.parametrize(
    "insert", ["frame = frame[:0]", "x1 = x2", "mutate(frame)", "box_size = 0"]
)
def test_source_binding_stops_on_intervening_changes(insert):
    code = ROI.replace("    roi =", "    " + insert + "\n    roi =")
    assert source_facts(code, "python") == []


def test_source_binding_does_not_infer_when_formula_or_names_differ():
    assert source_facts(ROI.replace("x1 + box_size", "x1 - box_size"), "python") == []
    assert source_facts(ROI.replace("x2, y2", "x1, y2"), "python") == []
    assert source_facts(ROI, "cpp") == []
    assert source_facts("def :", "python") == []


def test_deep_unknown_constructor_is_left_unanalysed():
    assert source_facts("model = a" + ".attribute" * 1500 + "()", "python") == []


def test_broadcast_calculator_agrees_with_numpy_assignment_including_leading_unit_axes():
    shapes = [(0,), (1,), (3,), (1, 3), (2, 3), (1, 2, 3), (3, 1), (2, 0)]
    for target, source in product(shapes, repeat=2):
        try:
            np.empty(target)[...] = np.zeros(source)
            expected = True
        except ValueError:
            expected = False
        result = check_broadcast(
            BroadcastArguments(target_shape=list(target), source_shape=list(source))
        )
        assert result["compatible"] == expected, (target, source)
    assert not check_broadcast(
        BroadcastArguments(target_shape=[80, 90, 3], source_shape=[100, 100, 3])
    )["compatible"]


@pytest.mark.parametrize(
    "name,args",
    [
        ("calculate_slice", {"shape": [2], "slices": [{"start": "__import__('os')"}]}),
        ("calculate_slice", {"shape": [10**12], "slices": [{}]}),
        ("calculate_slice", {"shape": [True], "slices": [{}]}),
        ("calculate_slice", {"shape": [3], "slices": [{"step": 0}]}),
        ("calculate_slice", {"shape": [3], "slices": [{}, {}]}),
        ("check_broadcast", {"target_shape": [-1], "source_shape": [3]}),
        ("keras_split", {"samples": 10, "validation_split": 1}),
        ("keras_split", {"samples": 10, "validation_split": 0.2, "path": ".env"}),
    ],
)
def test_calculators_reject_unbounded_or_executable_parameters(name, args):
    result = ToolRegistry(ReviewInput(code="x = 1")).execute(name, json.dumps(args))
    assert "error" in result


def test_keras_split_is_trailing_disjoint_holdout_before_shuffle():
    result = keras_split(SplitArguments(samples=700, validation_split=0.2))
    assert result["train_range"] == [0, 560]
    assert result["validation_range"] == [560, 700]
    assert result["overlap"] is False and result["random_split"] is False
    assert source_facts(KERAS, "python")[0]["active"] is True
    override = KERAS.replace("shuffle=True", "validation_data=(x, y)")
    assert source_facts(override, "python")[0]["active"] is False


@pytest.mark.parametrize(
    "code",
    [
        "model.fit(x, y, validation_split=0.2)",
        KERAS.replace("from keras import Sequential", "from custom import Sequential"),
        KERAS.replace("model.fit", "model = Custom()\nmodel.fit"),
        KERAS.replace("model = Sequential()", "Sequential = Custom\nmodel = Sequential()"),
        KERAS.replace("model.fit", "def f(model):\n    model.fit"),
        KERAS.replace("shuffle=True", "**options"),
    ],
)
def test_custom_shadowed_or_unknown_api_does_not_get_keras_contract(code):
    assert source_facts(code, "python") == []


@pytest.mark.parametrize(
    "code,text,evidence",
    [
        (ROI, "160×120 下 ROI 切片为空数组并导致崩溃", "roi = frame[y1:y2, x1:x2]"),
        (
            KERAS,
            "validation_split 从合并数据随机切分，验证集同源导致指标虚高",
            "model.fit(x, y, validation_split=0.2, shuffle=True)",
        ),
    ],
)
def test_wrong_claim_is_rejected_even_when_model_supports_it(code, text, evidence):
    request = ReviewInput(code=code)
    facts = source_facts(code, request.language)
    grounded, records = ground_candidates([claim(code, text, evidence)], request, {}, facts=facts)
    batch = AuditBatch.model_validate_json(audit_response()["content"])
    accepted, pending, final_records = finalize_candidates(
        grounded, batch, records, reflection_status="completed", facts=facts
    )
    assert not accepted and not pending
    assert final_records[-1].status == "rejected"
    assert "fact_1" in final_records[-1].reason


def test_bad_correction_cannot_reintroduce_roi_empty_claim():
    item = claim(ROI, "区域偏离预期中心", "roi = frame[y1:y2, x1:x2]")
    facts = source_facts(ROI, "python")
    grounded, records = ground_candidates([item], ReviewInput(code=ROI), {}, facts=facts)
    batch = AuditBatch.model_validate_json(
        audit_response(correction="160×120 下为空数组，所以崩溃")["content"]
    )
    accepted, pending, _ = finalize_candidates(
        grounded, batch, records, reflection_status="completed", facts=facts
    )
    assert not accepted and not pending


def test_facts_do_not_hide_real_shape_mismatch_or_explicit_validation_data_reuse():
    item = claim(FIXTURE, "100×100 赋值到 80×90 抛出 ValueError", "frame[:100, :100] = preview")
    assert not conflicting_fact(item, source_facts(FIXTURE, "python"))
    override = KERAS.replace("shuffle=True", "validation_data=(x, y)")
    item = claim(override, "验证和训练使用同一数组，发生样本重叠", "model.fit")
    assert not conflicting_fact(item, source_facts(override, "python"))


def test_three_calls_one_source_copy_and_stage_usage_reconciles():
    class MeteredClient(ScriptedClient):
        def complete(self, messages, **kwargs):
            self.metrics.prompt_tokens += 100
            self.metrics.completion_tokens += 20
            self.metrics.http_attempts += 1
            return super().complete(messages, **kwargs)

    batch = tool_response("parse_structure")
    batch["tool_calls"] += [
        {**tool_response("run_static_checks")["tool_calls"][0], "id": "call_2"},
        {**tool_response("read_source")["tool_calls"][0], "id": "call_3"},
    ]
    client = MeteredClient(
        [
            batch,
            draft_response([finding().model_copy(update={"rule_id": "AST005"})]),
            audit_response(),
        ]
    )
    report = ReviewAgent(client).review(ReviewInput(code="value = 1 / 0\n"))
    assert report.metrics.model_calls == 3
    for messages in client.messages:
        contexts = [json.loads(item["content"]) for item in messages if item["role"] == "user"]
        assert sum("numbered_source" in item for item in contexts) == 1
        assert all("source" not in item for item in contexts)
        assert all(
            "1: value = 1 / 0" not in item["content"] for item in messages if item["role"] == "tool"
        )
    for key in ("model_calls", "http_attempts", "prompt_tokens", "completion_tokens"):
        assert sum(getattr(stage, key) for stage in report.metrics.stages) == getattr(
            report.metrics, key
        )
    assert all(stage.elapsed_seconds >= 0 for stage in report.metrics.stages)


def test_additional_tool_round_retains_feedback_and_calculations_for_reviewer():
    args = json.dumps({"target_shape": [80, 90, 3], "source_shape": [100, 100, 3]})
    client = ScriptedClient(
        [
            tool_response("parse_structure"),
            tool_response("check_broadcast", args),
            draft_response([finding()]),
            audit_response(),
        ]
    )
    report = ReviewAgent(client).review(ReviewInput(code="value = 1 / 0\n"))
    assert report.metrics.model_calls == 4
    assert report.calculations[0]["result"]["compatible"] is False
    payload = json.loads(client.messages[-1][1]["content"])
    assert payload["calculations"] == report.calculations


def test_low_model_suggestion_skips_review_and_stays_pending():
    item = finding().model_copy(update={"severity": "low"})
    client = ScriptedClient([draft_response([item])])
    report = ReviewAgent(client).review(ReviewInput(code="value = 1 / 0\n"))
    assert report.metrics.model_calls == 1 and report.pending_findings
    assert not report.findings


def test_relocated_unused_variable_restores_actual_static_rule():
    code = "def f():\n    unused = 1\n    return 0\n"
    registry = ToolRegistry(ReviewInput(code=code))
    result = registry.execute("run_static_checks", "{}")
    canonical = next(item for item in result["ruff"]["findings"] if item["rule_id"] == "RUFF:F841")
    item = claim(code, "夸大模型说法", "unused = 1").model_copy(update={"rule_id": "RUFF:F841"})
    grounded, records = ground_candidates([item], registry.request, registry.results)
    assert grounded[0].static and grounded[0].finding.title == canonical["title"]
    assert not any(item.status == "rule_removed" for item in records)
