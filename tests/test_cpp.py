import json
from pathlib import Path
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from review_agent.agent import ReviewAgent
from review_agent.cpp_tools import cpp_checks, cpp_structure
from review_agent.files import decode_source, read_source_file
from review_agent.models import Draft, Metrics, ReviewInput
from review_agent.reporting import to_markdown
from review_agent.tools import ToolRegistry


@pytest.mark.parametrize(
    "filename", ["a.cpp", "a.cc", "a.cxx", "a.hpp", "a.hh", "a.hxx", "a.h", "a.CPP"]
)
def test_cpp_file_types(filename):
    assert ReviewInput(code="int value = 1;", filename=filename).language == "cpp"


def test_python_and_unsupported_file_type():
    assert ReviewInput(code="value = 1", filename="a.PY").language == "python"
    with pytest.raises(ValidationError, match="支持 Python"):
        ReviewInput(code="anything", filename="a.exe")


def test_cpp_decode_uses_utf8_not_python_encoding_directives(tmp_path):
    code = '// coding: nonsense\nconst char* text = "你好";\n'
    assert decode_source(b"\xef\xbb\xbf" + code.encode("utf-8"), "a.cpp") == code
    path = tmp_path / "a.hpp"
    path.write_bytes(code.encode("utf-8"))
    assert read_source_file(path) == code
    with pytest.raises(ValueError, match="UTF-8"):
        decode_source(b"\xff\xfe", "a.cpp")


@pytest.mark.parametrize(
    "code, rule",
    [
        ("int f(int x) { return x / 0; }", "CPP002"),
        ("int f(int x) { return x % (0u); }", "CPP002"),
        ("void f(char* dst, const char* src) { std::strcpy(dst, src); }", "CPP003"),
        ("bool f(int x) { if (x = 1) return true; return false; }", "CPP004"),
        ("void f() { try { run(); } catch (...) { /* nothing */ } }", "CPP005"),
        ("void f() { int* values = new int[8]; delete values; }", "CPP006"),
        ("void f() { int* value = new int; delete[] value; }", "CPP006"),
        ("int main( { return 0; }", "CPP001"),
    ],
)
def test_cpp_defects_have_real_locations(code, rule):
    result = cpp_checks(code)
    assert rule in {item["rule_id"] for item in result["builtin_findings"]}
    assert all(item["line_start"] == 1 for item in result["builtin_findings"])


@pytest.mark.parametrize(
    "code",
    [
        "void f() { int* values = new int[8]; delete[] values; }",
        "void f() { int* value = new int; delete value; }",
        "bool f(int x) { if (x == 1) return true; return false; }",
        "bool f(int x) { if ((x = next())) return true; return false; }",
        "double f() { return 1.0 / 0; }",
        'const char* s = "strcpy(x, y); x / 0"; // if (x = 1)\n',
        'const char* s = R"code(delete x; strcpy(x, y); x / 0)code";\n',
    ],
)
def test_cpp_rules_do_not_flag_comments_literals_or_known_correct_pairs(code):
    result = cpp_checks(code)
    assert result["builtin_status"] == "completed"
    assert not result["builtin_findings"]


def test_cpp_structure_includes_functions_and_classes_without_loading_headers():
    code = '#include "not-present.hpp"\nnamespace demo { class Order { public: int total() { return 1; } }; }'
    result = cpp_structure(code)
    assert result["status"] == "completed"
    assert {symbol["name"] for symbol in result["symbols"]} >= {"demo", "Order", "total"}
    assert result["imports"][0]["line"] == 1
    assert result["limitations"]


def test_cpp_dispatch_never_invokes_python_checker_or_external_commands():
    registry = ToolRegistry(
        ReviewInput(code="int f(int x) { return x / 0; }", filename="sample.cpp")
    )
    with (
        patch("review_agent.tools.ruff_checks", side_effect=AssertionError("Python-only tool")),
        patch("review_agent.tools.parse_tree", side_effect=AssertionError("Python-only parser")),
        patch("review_agent.tools.subprocess.run", side_effect=AssertionError("No compile/run")),
    ):
        assert registry.execute("parse_structure", "{}")["language"] == "cpp"
        assert registry.execute("run_static_checks", "{}")["builtin_findings"]
    assert not any("Python" in name or "Ruff" in name for name in registry.checks)


def test_cpp_parser_missing_is_reported_as_incomplete():
    with patch("review_agent.cpp_tools.cpp_language", side_effect=ImportError("missing")):
        report = ReviewAgent().review(ReviewInput(code="int x = 1;", filename="a.cpp"))
    assert report.checks["C++ 本地规则"] == "unavailable"
    assert any("检查未完成" in item for item in report.limitations)


def test_cpp_demo_report_and_markdown_fence():
    path = Path(__file__).resolve().parents[1] / "examples" / "buggy_order.cpp"
    request = ReviewInput(code=read_source_file(path), filename=path.name)
    report = ReviewAgent().review(request)
    assert report.language == "cpp"
    assert {f.rule_id for f in report.findings} == {
        "CPP002",
        "CPP003",
        "CPP004",
        "CPP005",
        "CPP006",
    }
    assert report.metrics.model_calls == 0
    assert "````cpp\n" in to_markdown(report, request)
    assert "不进行编译" in "".join(report.limitations)


def test_cpp_model_and_followup_receive_language_context():
    class Recorder:
        def __init__(self):
            self.metrics = Metrics()
            self.payloads = []

        def complete(self, messages, **kwargs):
            self.payloads.append(messages)
            if "require_patch" in json.loads(messages[1]["content"]):
                from test_agent import followup_response

                return followup_response("这是一份 C++ 报告。")
            if kwargs.get("json_output"):
                return {
                    "role": "assistant",
                    "content": Draft(summary="完成", findings=[]).model_dump_json(),
                }
            return {"role": "assistant", "content": "这是一份 C++ 报告。"}

    model = Recorder()
    request = ReviewInput(code="int x = 1;", filename="a.cpp")
    agent = ReviewAgent(model)
    report = agent.review(request)
    assert report.language == "cpp"
    for messages in model.payloads:
        assert json.loads(messages[1]["content"])["language"] == "cpp"
    agent.follow_up(request, report, "解释一下")
    assert json.loads(model.payloads[-1][1]["content"])["language"] == "cpp"


def test_filename_change_invalidates_followup():
    request = ReviewInput(code="# valid comment", filename="a.py")
    report = ReviewAgent().review(request)
    with pytest.raises(ValueError, match="代码已更改"):
        ReviewAgent().follow_up(ReviewInput(code=request.code, filename="a.cpp"), report, "解释")
