import json
import subprocess
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from review_agent.files import decode_source
from review_agent.models import ReviewInput
from review_agent.tools import ToolRegistry, builtin_checks, ruff_checks


@pytest.mark.parametrize(
    "code, rule",
    [
        ("def f(x=[]):\n    x.append(1)\n", "AST002"),
        ("try:\n    x = 1\nexcept:\n    pass\n", "AST003"),
        ("try:\n    x = 1\nexcept ValueError:\n    pass\n", "AST004"),
        ("x = 1 / 0\n", "AST005"),
        ("x = eval(user_input)\n", "AST006"),
        ("def broken(:\n", "AST001"),
    ],
)
def test_known_static_defects(code, rule):
    findings, _ = builtin_checks(code)
    assert rule in {item.rule_id for item in findings}


def test_clean_guard_is_not_flagged():
    code = "def average(values):\n    if not values:\n        return 0\n    return sum(values) / len(values)\n"
    assert builtin_checks(code) == ([], "completed")


def test_source_is_never_executed(tmp_path):
    marker = tmp_path / "executed.txt"
    code = f"open({str(marker)!r}, 'w').write('should not happen')\n"
    tools = ToolRegistry(ReviewInput(code=code))
    tools.execute("parse_structure", "{}")
    tools.execute("run_static_checks", "{}")
    assert not marker.exists()


@pytest.mark.parametrize(
    "name, arguments",
    [
        ("read_file", '{"path":"C:/secret.env"}'),
        ("read_source", '{"path":"../.env"}'),
        ("read_source", '{"start_line":0}'),
        ("read_source", '{"start_line":true}'),
        ("read_source", '{"start_line":2,"end_line":1}'),
        ("read_source", '{"end_line":999}'),
        ("run_static_checks", "not json"),
        ("parse_structure", "[]"),
    ],
)
def test_tool_arguments_are_rejected(name, arguments):
    registry = ToolRegistry(ReviewInput(code="value = 1\n"))
    assert "error" in registry.execute(name, arguments)


@pytest.mark.parametrize(
    "code",
    ["", "   ", "a\x00b", "#" * 48001, "x=1\n" * 801],
    ids=["empty", "whitespace", "null", "too-many-bytes", "too-many-lines"],
)
def test_input_limits(code):
    with pytest.raises(ValidationError):
        ReviewInput(code=code)


def test_bom_and_encoding_declaration():
    assert decode_source(b"\xef\xbb\xbfvalue = 1\n") == "value = 1\n"
    assert "你好" in decode_source("# coding: gbk\nmessage = '你好'\n".encode("gbk"))
    with pytest.raises(ValueError):
        decode_source(b"\xff\xfe\x00")


def test_ruff_timeout_is_not_clean_result():
    with patch(
        "review_agent.tools.subprocess.run", side_effect=subprocess.TimeoutExpired("ruff", 12)
    ):
        assert ruff_checks("x = 1")["status"] == "timeout"


def test_ruff_missing_is_visible():
    with patch(
        "review_agent.tools.subprocess.run",
        return_value=subprocess.CompletedProcess([], 1, "", "No module named ruff"),
    ):
        assert ruff_checks("x = 1")["status"] != "completed"


def test_ruff_real_undefined_variable():
    result = ruff_checks("def total():\n    return missing_value\n")
    assert result["status"] == "completed"
    assert any(f["rule_id"] == "RUFF:F821" for f in result["findings"])


def test_numbered_read_and_structure():
    registry = ToolRegistry(ReviewInput(code="def add(a, b):\n    return a + b\n"))
    assert (
        registry.execute("read_source", json.dumps({"start_line": 2}))["source"]
        == "2:     return a + b"
    )
    assert registry.execute("parse_structure", "{}")["symbols"][0]["name"] == "add"
