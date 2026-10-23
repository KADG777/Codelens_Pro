"""Exercise the real Streamlit page without a browser or model requests."""

from contextlib import closing
from copy import deepcopy
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest

import ui.progress as progress_ui
from review_agent.config import Settings
from workflow import WorkflowRunService
from workflow.examples import BUGGY_ORDERS, CORRECT_ORDERS, ORDER_TASK

APP_PATH = Path(__file__).resolve().parents[1] / "app.py"


def assert_no_page_error(app: AppTest) -> None:
    assert not app.exception, [item.value for item in app.exception]


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("CODELENS_FLOW_DATA_DIR", str(tmp_path))
    # Keep tests independent of local credentials and avoid live network calls.
    monkeypatch.setattr(Settings, "from_env", classmethod(lambda cls: cls(api_key="")))
    app = AppTest.from_file(str(APP_PATH), default_timeout=45).run()
    assert_no_page_error(app)
    return app, tmp_path


def snapshot(app: AppTest, directory: Path) -> dict:
    with closing(WorkflowRunService(directory / "workflow.sqlite3")) as service:
        return service.get_snapshot(app.session_state["flow_active_run"])


def comparison_markup(app: AppTest) -> str:
    frames = app.get("iframe")
    assert frames, "代码工作区应直接显示修改前后对比"
    return frames[0].proto.srcdoc


def test_default_generation_shows_diff_and_downloads_complete_current_code(workspace, monkeypatch):
    app, directory = workspace
    downloads = []
    original_download = st.download_button

    def capture_download(*args, **kwargs):
        downloads.append(kwargs)
        return original_download(*args, **kwargs)

    monkeypatch.setattr(st, "download_button", capture_download)
    assert app.selectbox(key="flow_profile").value == "仅静态检查"
    app.button(key="flow_start").click().run()
    assert_no_page_error(app)
    state = snapshot(app, directory)
    assert state["status"] == "completed_static"
    assert len(state["versions"]) == 2
    assert state["versions"][0]["code"] == BUGGY_ORDERS
    assert state["versions"][-1]["code"] == CORRECT_ORDERS
    assert not any(tab.label == "更改对比" for tab in app.tabs)
    assert "修改前源码" in comparison_markup(app)
    assert "修改后源码" in comparison_markup(app)
    assert "绿色：新增" in comparison_markup(app)
    assert len(app.get("download_button")) == 2
    code_download = next(item for item in downloads if item["file_name"] == "solution.py")
    assert code_download["data"].decode("utf-8") == state["versions"][-1]["code"]
    assert any(item["file_name"].endswith(".md") for item in downloads)
    metrics = {item.label: item.value for item in app.metric}
    assert metrics["模型调用"] == "0"
    assert metrics["模型用量"] == "0"
    assert any("功能验收" in warning.value for warning in app.warning)
    assert not any(button.key == "flow_start" for button in app.button)
    assert any("本次结果摘要" in item.value for item in app.markdown)
    assert any("流程实时状态" in item.value for item in app.markdown)


def test_pasted_source_skips_coding_agent(workspace):
    app, directory = workspace
    app.radio(key="flow_entry").set_value("上传 / 粘贴代码").run()
    assert_no_page_error(app)
    assert app.text_area(key="flow_source").value == BUGGY_ORDERS
    app.button(key="flow_start").click().run()
    assert_no_page_error(app)
    state = snapshot(app, directory)
    assert state["input_mode"] == "upload"
    assert state["status"] == "completed_static"
    assert not any(event["node"] == "coding" for event in state["events"])
    assert any(event["node"] == "review" for event in state["events"])
    assert any(event["node"] == "fix" for event in state["events"])
    assert state["versions"][0]["origin"] == "upload"


def test_paused_review_resumes_saved_run_without_recoding(workspace):
    app, directory = workspace
    app.checkbox(key="flow_pause").check().run()
    app.button(key="flow_start").click().run()
    assert_no_page_error(app)
    before = snapshot(app, directory)
    run_id = before["run_id"]
    assert before["status"] == "paused"
    assert before["pending_nodes"]
    assert len(before["versions"]) == 1
    assert "两侧源码相同" in comparison_markup(app)
    assert "summarize_orders" in comparison_markup(app)
    app.button(key="flow_resume").click().run()
    assert_no_page_error(app)
    after = snapshot(app, directory)
    assert after["run_id"] == run_id
    assert after["status"] == "completed_static"
    assert len(after["versions"]) == 2
    assert sum(event["node"] == "coding" for event in after["events"]) == 1


def test_edit_current_source_starts_a_new_run_with_new_evidence(workspace):
    app, directory = workspace
    app.button(key="flow_start").click().run()
    first = snapshot(app, directory)
    app.button(key="flow_edit_" + first["run_id"]).click().run()
    assert_no_page_error(app)
    assert "flow_active_run" not in app.session_state
    assert app.radio(key="flow_entry").value == "上传 / 粘贴代码"
    assert app.text_area(key="flow_source").value == CORRECT_ORDERS
    assert not app.metric
    assert not any(tab.label == "审查结果" for tab in app.tabs)
    edited = CORRECT_ORDERS + "\n# 用户补充的注释\n"
    app.text_area(key="flow_source").set_value(edited).run()
    app.button(key="flow_start").click().run()
    assert_no_page_error(app)
    second = snapshot(app, directory)
    assert second["run_id"] != first["run_id"]
    assert second["versions"][0]["code"] == edited
    assert second["review"]["code_hash"] == second["versions"][-1]["hash"]
    assert second["review"]["code_hash"] != first["review"]["code_hash"]


def test_live_source_uses_general_default_but_keeps_explicit_user_goal(workspace):
    app, _ = workspace
    app.radio(key="flow_mode").set_value("真实模型").run()
    app.radio(key="flow_entry").set_value("上传 / 粘贴代码").run()
    assert_no_page_error(app)
    assert app.text_area(key="flow_task").value != ORDER_TASK
    assert "保留" in app.text_area(key="flow_task").value
    assert app.text_area(key="flow_source").value == ""
    goal = "提升识别准确性能"
    app.text_area(key="flow_task").set_value(goal).run()
    app.radio(key="flow_mode").set_value("流程演示（无需密钥）").run()
    app.radio(key="flow_mode").set_value("真实模型").run()
    assert app.text_area(key="flow_task").value == goal


def test_unchanged_repair_shows_full_comparison_and_agent_reason(workspace, monkeypatch):
    app, directory = workspace
    app.selectbox(key="flow_scenario").set_value("修复无进展").run()
    app.button(key="flow_start").click().run()
    assert_no_page_error(app)
    state = snapshot(app, directory)
    assert state["termination_reason"] == "no_progress"
    assert len(state["versions"]) == 1
    reason = "尚未处理审查发现的数据泄露，当前候选源码与输入完全一致。"
    state["repair_records"][-1]["change_summary"] = "候选未产生修改。"
    state["repair_records"][-1]["unresolved_items"] = [reason]
    original_get = WorkflowRunService.get_snapshot

    def get_with_reason(service, run_id):
        return deepcopy(state) if run_id == state["run_id"] else original_get(service, run_id)

    monkeypatch.setattr(WorkflowRunService, "get_snapshot", get_with_reason)
    app.run()
    assert_no_page_error(app)
    assert any("修复未生效" in item.value for item in app.warning)
    assert any(reason in item.value for item in app.markdown)
    markup = comparison_markup(app)
    assert "两侧源码相同" in markup
    assert markup.count("summarize_orders") == 2
    assert '<tr class="removed"' not in markup
    assert '<tr class="added"' not in markup


def test_explicit_retry_uses_saved_goal_and_runtime_connection(workspace, monkeypatch):
    app, directory = workspace
    app.selectbox(key="flow_scenario").set_value("修复无进展").run()
    app.button(key="flow_start").click().run()
    state = snapshot(app, directory)
    state["mode"] = "live"
    state["task"] = "提升识别准确性能"
    state["review"]["requirements_status"] = "met"
    state["review"]["requirements_reason"] = "旧版仅根据源码注释判断已改进。"
    old_id = state["run_id"]
    new_id = "ui-retry-associated-run"
    retried = {**deepcopy(state), "run_id": new_id, "parent_run_id": old_id}
    calls = {}
    original_get = WorkflowRunService.get_snapshot

    def fake_get(service, run_id):
        if run_id == old_id:
            return deepcopy(state)
        if run_id == new_id:
            return deepcopy(retried)
        return original_get(service, run_id)

    def fake_retry(service, run_id, **kwargs):
        calls["retry"] = (run_id, kwargs)
        return new_id

    def fake_execute(service, run_id, settings=None, **kwargs):
        calls["execute"] = (run_id, settings)
        return deepcopy(retried)

    monkeypatch.setattr(WorkflowRunService, "get_snapshot", fake_get)
    monkeypatch.setattr(WorkflowRunService, "retry_repair", fake_retry)
    monkeypatch.setattr(WorkflowRunService, "execute", fake_execute)
    app.run()
    assert not any("当前为预设流程演示" in item.value for item in app.info)
    assert any("未实测准确率或性能" in item.value for item in app.warning)
    assert any("需求核对：尚无法确认" in item.value for item in app.caption)
    app.text_input(key="flow_api_key").set_value("runtime-test-key").run()
    # Sidebar still selects demo for new tasks; restoring a live task uses its mode.
    assert app.radio(key="flow_mode").value == "流程演示（无需密钥）"
    app.button(key="flow_retry_repair").click().run()
    assert_no_page_error(app)
    assert app.session_state["flow_active_run"] == new_id
    assert calls["retry"] == (old_id, {"max_fix_rounds": 2})
    assert calls["execute"][0] == new_id
    assert calls["execute"][1].api_key == "runtime-test-key"
    assert snapshot(app, directory)["task"] == "提升识别准确性能"


def test_empty_task_and_missing_live_key_show_readable_errors(workspace):
    app, _ = workspace
    app.text_area(key="flow_task").set_value("").run()
    app.button(key="flow_start").click().run()
    assert_no_page_error(app)
    assert any("目标" in error.value for error in app.error)
    assert "flow_active_run" not in app.session_state
    app.text_area(key="flow_task").set_value(ORDER_TASK).run()
    app.radio(key="flow_mode").set_value("真实模型").run()
    app.button(key="flow_start").click().run()
    assert_no_page_error(app)
    assert any("API" in error.value or "密钥" in error.value for error in app.error)
    assert "flow_active_run" not in app.session_state


def test_run_link_opens_saved_record_and_new_task_clears_the_link(workspace):
    app, directory = workspace
    app.button(key="flow_start").click().run()
    saved_id = snapshot(app, directory)["run_id"]
    assert "run" in app.query_params
    linked = AppTest.from_file(str(APP_PATH), default_timeout=45)
    linked.query_params["run"] = saved_id
    linked.run()
    assert_no_page_error(linked)
    assert linked.session_state["flow_active_run"] == saved_id
    assert "修改前源码" in comparison_markup(linked)
    assert any("记录链接" in item.value for item in linked.caption)
    linked.button(key="flow_new_task").click().run()
    assert_no_page_error(linked)
    assert "run" not in linked.query_params
    assert "flow_active_run" not in linked.session_state
    linked.run()
    assert "flow_active_run" not in linked.session_state
    assert linked.button(key="flow_start")
    linked.query_params["run"] = "missing-record"
    linked.run()
    assert_no_page_error(linked)
    assert "flow_active_run" not in linked.session_state
    assert any("不存在" in item.value for item in linked.error)


def test_visual_editor_drives_real_parallel_plan_and_live_active_cards(workspace, monkeypatch):
    app, directory = workspace
    observed = []
    original = progress_ui.build_progress_html

    def record_progress(state):
        observed.append(deepcopy(state))
        return original(state)

    monkeypatch.setattr(progress_ui, "build_progress_html", record_progress)
    app.button(key="flow_node_fix").click().run()
    assert any("④ 修复 Agent" in item.value for item in app.markdown)
    app.radio(key="flow_execution_order").set_value("并行执行").run()
    app.button(key="flow_start").click().run()
    assert_no_page_error(app)
    state = snapshot(app, directory)
    assert state["workflow_config"]["parallel_checks"] is True
    assert state["status"] == "completed_static"
    assert any(set(item.get("active_nodes", [])) >= {"review", "validate"} for item in observed)
    current = state["versions"][-1]["hash"]
    assert state["review"]["code_hash"] == state["validation"]["code_hash"] == current
    assert any("并行检查" in item.value for item in app.markdown)


def test_visual_editor_disable_repair_changes_execution_path(workspace):
    app, directory = workspace
    app.checkbox(key="flow_enable_repair").uncheck().run()
    app.button(key="flow_start").click().run()
    assert_no_page_error(app)
    state = snapshot(app, directory)
    assert state["workflow_config"]["enable_repair"] is False
    assert len(state["versions"]) == 1
    assert state["fix_round"] == 0
    assert not any(event.get("node") == "fix" for event in state["events"])


def test_problem_selection_focuses_version_bound_change_instead_of_asserting_success(workspace):
    app, directory = workspace
    app.button(key="flow_start").click().run()
    state = snapshot(app, directory)
    selector = app.selectbox(key="flow_issue_" + state["run_id"])
    from ui.evidence import issue_traces

    trace = issue_traces(state)[0]
    selector.set_value(trace["key"]).run()
    assert_no_page_error(app)
    assert any("复查未再检出同类问题" in item.value for item in app.markdown)
    assert any("功能未执行" in item.value for item in app.caption)
    assert 'id="old-L' in comparison_markup(app)
    assert any("修复 Agent 声明" in item.value for item in app.markdown)


def test_second_registered_demo_loads_explicit_contract(workspace):
    app, directory = workspace
    app.selectbox(key="flow_task_spec").set_value("text-stats-v1").run()
    app.button(key="flow_load_example").click().run()
    assert "text_stats" in app.text_area(key="flow_task").value
    app.button(key="flow_start").click().run()
    assert_no_page_error(app)
    state = snapshot(app, directory)
    assert state["acceptance_id"] == "text-stats-v1"
    assert len(state["versions"]) == 2
    assert state["status"] == "completed_static"


def test_custom_source_functional_without_contract_is_blocked_at_submission(workspace):
    app, directory = workspace
    app.radio(key="flow_entry").set_value("上传 / 粘贴代码").run()
    app.text_area(key="flow_task").set_value("审查自定义函数").run()
    app.session_state["flow_acceptance_id"] = None
    app.selectbox(key="flow_profile").set_value("隔离功能验收（便携运行时）").run()
    app.button(key="flow_start").click().run()
    assert_no_page_error(app)
    assert any("绑定验收合同" in item.value for item in app.error)
    assert "flow_active_run" not in app.session_state
    with closing(WorkflowRunService(directory / "workflow.sqlite3")) as service:
        assert service.list_runs() == []


def test_node_selection_and_failed_validation_trace_render_without_source_lines(workspace):
    app, directory = workspace
    app.multiselect(key="flow_extensions").set_value(["acceptance_explainer"]).run()
    app.button(key="flow_start").click().run()
    state = snapshot(app, directory)
    app.button(key=f"inspect_{state['run_id']}_acceptance_explainer").click().run()
    assert_no_page_error(app)
    assert any("预设解读" in item.value for item in app.markdown)
    from ui.evidence import issue_traces
    trace = next(t for t in issue_traces(state) if t.get("kind") == "validation")
    app.selectbox(key=f"flow_issue_{state['run_id']}").set_value(trace["key"]).run()
    assert_no_page_error(app)
    assert any("同一用例重新验收通过" in item.value for item in app.caption)
    assert "修改后源码" in comparison_markup(app)
