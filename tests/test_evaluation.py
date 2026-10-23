import pytest

from score_evaluation import score_run


def fixture():
    return [
        {
            "id": "a",
            "expected": [{"issue": "a"}, {"issue": "b"}],
            "report": {
                "findings": [{}],
                "pending_findings": [{}, {}],
                "metrics": dict(
                    elapsed_seconds=1.5, model_calls=2, prompt_tokens=100, completion_tokens=50
                ),
            },
        }
    ]


def test_scoring_does_not_hide_pending_noise_or_missing_defects():
    labels = {
        "a": {
            "findings:1": dict(outcome="matched", expected_index=1, reason="正确机制"),
            "pending_findings:1": dict(outcome="incorrect", reason="实际索引合法"),
            "pending_findings:2": dict(outcome="advice", reason="可选风格建议"),
        }
    }
    result = score_run(fixture(), labels)
    assert result["recall"] == 0.5
    assert result["known_defect_fraction"] == 1 / 3
    assert result["formal_supported_fraction"] == 1
    assert result["counts"]["all"]["incorrect"] == 1
    labels["a"].pop("pending_findings:1")
    with pytest.raises(ValueError, match="每条"):
        score_run(fixture(), labels)


def test_failed_cases_still_count_toward_recall():
    result = score_run([{"id": "a", "expected": [{}], "error": "network"}], {"a": {}})
    assert result["recall"] == 0 and result["completed"] == 0
