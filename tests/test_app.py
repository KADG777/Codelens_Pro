from pathlib import Path
from unittest.mock import patch

import pytest
from streamlit.testing.v1 import AppTest
from test_agent import ScriptedClient, draft_response, finding, followup_response

from review_agent.agent import ReviewAgent
from review_agent.models import ReviewInput


@pytest.mark.parametrize("blocked", [False, True])
def test_web_followup_shows_validation_status_and_history(blocked):
    request = ReviewInput(code="value = 1\n", filename="sample.py")
    report = (
        ReviewAgent().review(request).model_copy(update={"mode": "deepseek", "model": "test-model"})
    )
    app = AppTest.from_file(
        str(Path(__file__).resolve().parents[1] / "pages/1_原版代码审查.py"), default_timeout=30
    ).run()
    app.text_area(key="editor").set_value(request.code)
    app.text_input(key="filename").set_value(request.filename)
    app.radio(key="mode").set_value("DeepSeek API").run()
    app.text_input(key="api_key").set_value("test-key")
    next(item for item in app.text_input if item.label == "模型名称").set_value("test-model")
    app.session_state["report"] = report
    app.session_state["request"] = request
    app.run()
    client = ScriptedClient(
        [followup_response("代码定义了 value。", references=["finding:999"] if blocked else [])] * 2
    )
    client.close = lambda: None
    with patch("review_agent.llm.DeepSeekClient", return_value=client):
        app.chat_input[0].set_value("只解释这段代码").run()
    assert not app.exception
    history = app.session_state["history"]
    assert len(history) == 2 and history[0]["role"] == "user"
    assert history[1]["review_meta"]["status"] == ("blocked" if blocked else "completed")
    assert any("本轮校验记录" == item.label for item in app.expander)
    assert any("次模型调用" in item.value for item in app.caption)
    assert history[1]["review_meta"]["attempts"]
    assert any("第 1 次候选" in item.value for item in app.caption)


def test_web_demo_review_and_changed_source_notice():
    app = AppTest.from_file(
        str(Path(__file__).resolve().parents[1] / "pages/1_原版代码审查.py"), default_timeout=30
    ).run()
    assert not app.exception
    next(button for button in app.button if button.label == "开始审查 →").click().run()
    assert not app.exception
    assert app.session_state["report"].mode == "demo"
    assert len(app.session_state["report"].findings) >= 3
    assert len(app.metric) == 4
    assert len(app.dataframe) == 1
    assert "耗时（秒）" in app.dataframe[0].value.columns
    app.text_area(key="editor").set_value("value = 1\n").run()
    assert any("编辑区内容已变化" in warning.value for warning in app.warning)


def test_web_missing_key_is_actionable():
    app = AppTest.from_file(
        str(Path(__file__).resolve().parents[1] / "pages/1_原版代码审查.py"), default_timeout=30
    ).run()
    app.radio(key="mode").set_value("DeepSeek API").run()
    app.text_input(key="api_key").set_value("").run()
    next(button for button in app.button if button.label == "开始审查 →").click().run()
    assert not app.exception
    assert any("DEEPSEEK_API_KEY" in error.value for error in app.error)


def test_web_cpp_example_and_switch_back_to_python():
    app = AppTest.from_file(
        str(Path(__file__).resolve().parents[1] / "pages/1_原版代码审查.py"), default_timeout=30
    ).run()
    app.selectbox(key="example_language").set_value("C++").run()
    next(button for button in app.button if button.label == "载入问题示例").click().run()
    assert app.text_input(key="filename").value == "buggy_order.cpp"
    next(button for button in app.button if button.label == "开始审查 →").click().run()
    assert not app.exception
    assert app.session_state["report"].language == "cpp"
    assert len(app.session_state["report"].findings) == 5
    next(button for button in app.button if button.label == "载入改进示例").click().run()
    next(button for button in app.button if button.label == "开始审查 →").click().run()
    assert app.session_state["report"].findings == []
    app.selectbox(key="example_language").set_value("Python").run()
    next(button for button in app.button if button.label == "载入问题示例").click().run()
    next(button for button in app.button if button.label == "开始审查 →").click().run()
    assert app.session_state["report"].language == "python"
    assert not app.exception


def test_web_pending_claim_shares_list_and_retains_pending_label():
    request = ReviewInput(code="value = 1 / 0\n", filename="sample.py")
    report = ReviewAgent(ScriptedClient([draft_response([finding()])])).review(request, "direct")
    app = AppTest.from_file(
        str(Path(__file__).resolve().parents[1] / "pages/1_原版代码审查.py"), default_timeout=30
    ).run()
    app.text_area(key="editor").set_value(request.code)
    app.text_input(key="filename").set_value(request.filename)
    with patch("review_agent.agent.ReviewAgent.review", return_value=report):
        next(button for button in app.button if button.label == "开始审查 →").click().run()
    assert not app.exception
    assert [metric.value for metric in app.metric[:3]] == ["0", "1", "0"]
    assert any(tab.label == "问题与建议（1）" for tab in app.tabs)
    assert not any(tab.label.startswith("待确认") for tab in app.tabs)
    assert any("01 · 中优先级 · 待确认" in item.label for item in app.expander)
    assert any("未进行模型复核" in item.value for item in app.markdown)


def test_web_and_markdown_expose_source_bound_computation():
    from test_semantics import ROI

    from review_agent.reporting import to_markdown

    request = ReviewInput(code=ROI, filename="crop.py")
    report = ReviewAgent().review(request)
    exported = to_markdown(report, request)
    assert "[40, 20, 3]" in exported and "empty=False" in exported
    assert "各阶段开销" in exported
    app = AppTest.from_file(
        str(Path(__file__).resolve().parents[1] / "pages/1_原版代码审查.py"), default_timeout=30
    ).run()
    app.text_area(key="editor").set_value(request.code)
    app.text_input(key="filename").set_value(request.filename)
    with patch("review_agent.agent.ReviewAgent.review", return_value=report):
        next(button for button in app.button if button.label == "开始审查 →").click().run()
    assert not app.exception
    assert any("API 依据" in item.label for item in app.expander)


def test_web_shows_both_preprocessing_citations_and_trigger():
    from test_claim_checks import PAIR, comparison, inference_ref

    from review_agent.models import ReviewedFinding

    request = ReviewInput(code=PAIR, filename="pair.py")
    report = ReviewAgent().review(request)
    item = comparison(
        related_evidence=[inference_ref()],
        trigger="分别使用 train 与 infer 路径时；精度影响未测试。",
        trigger_basis="local_input",
    )
    report.findings = [ReviewedFinding(**item.model_dump(), verification="reviewed")]
    app = AppTest.from_file(
        str(Path(__file__).resolve().parents[1] / "pages/1_原版代码审查.py"), default_timeout=30
    ).run()
    app.text_area(key="editor").set_value(request.code)
    app.text_input(key="filename").set_value(request.filename)
    with patch("review_agent.agent.ReviewAgent.review", return_value=report):
        next(button for button in app.button if button.label == "开始审查 →").click().run()
    assert not app.exception
    assert any("训练端 · L1–L1" in item.value for item in app.markdown)
    assert any("推理端 · L2–L2" in item.value for item in app.markdown)
    assert any("具体触发条件" in item.value for item in app.markdown)
