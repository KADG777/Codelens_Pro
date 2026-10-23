"""Allowlisted tools. Submitted source is parsed, never imported or executed."""

import ast
import json
import subprocess
import sys
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from review_agent.cpp_tools import cpp_checks, cpp_structure
from review_agent.models import Finding, ReviewInput
from review_agent.selection import select_findings, selection_notice
from review_agent.semantics import (
    BroadcastArguments,
    SliceArguments,
    SplitArguments,
    calculate_slice,
    check_broadcast,
    keras_split,
    source_facts,
)


class ReadArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    start_line: int = Field(default=1, ge=1, strict=True)
    end_line: int | None = Field(default=None, ge=1, strict=True)


class NoArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


ARGUMENTS = {
    "read_source": ReadArguments,
    "parse_structure": NoArguments,
    "run_static_checks": NoArguments,
    "check_semantics": NoArguments,
    "calculate_slice": SliceArguments,
    "check_broadcast": BroadcastArguments,
    "keras_split": SplitArguments,
}
DESCRIPTIONS = {
    "read_source": "读取本次提交的 Python 或 C++ 源码，可指定行号范围；不接受文件路径。",
    "parse_structure": "按文件语言解析结构：Python 使用 AST，C++ 使用 Tree-sitter。返回符号和解析诊断；不执行代码或读取头文件。",
    "run_static_checks": "按语言运行静态检查：Python AST/Ruff，C++ 语法树规则。返回证据和覆盖限制；不编译或运行代码。",
    "check_semantics": "读取程序从本文件窄模式提取的切片事实和 Keras API 合同；不执行被审查代码。",
    "calculate_slice": "给定小范围整数 shape 和 slices，计算 NumPy 基本切片的归一化边界、结果形状和空值。不接受表达式/源码。",
    "check_broadcast": "判断 source_shape 能否赋值广播到 target_shape；只计算形状，不分配数组。",
    "keras_split": "按 Keras validation_split 合同计算给定样本数的训练/验证索引范围，附官方出处；不加载 Keras。",
}
TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": name,
            "description": DESCRIPTIONS[name],
            "parameters": schema.model_json_schema(),
        },
    }
    for name, schema in ARGUMENTS.items()
]


def parse_tree(code: str) -> tuple[ast.Module | None, dict | None]:
    try:
        return ast.parse(code, filename="review.py"), None
    except SyntaxError as exc:
        return None, {"message": exc.msg, "line": exc.lineno or 1, "offset": exc.offset}
    except (ValueError, RecursionError, MemoryError):
        return None, {"message": "无法解析：代码结构过深或过于复杂。", "line": 1}


def builtin_checks(code: str) -> tuple[list[Finding], str]:
    tree, error = parse_tree(code)
    if error:
        line = min(error["line"], len(code.splitlines()))
        return [
            Finding(
                title="Python 语法无法解析",
                severity="high",
                category="Bug",
                line_start=line,
                line_end=line,
                explanation=f"解析器报告：{error['message']}。该文件无法作为正常 Python 模块解析。",
                evidence=f"ast.parse 在第 {line} 行返回语法错误。",
                suggestion="先修复该处语法并重新审查；语法错误可能导致后续问题未被发现。",
                rule_id="AST001",
            )
        ], "syntax_error"
    findings: list[Finding] = []

    def add(
        node: ast.AST,
        rule: str,
        title: str,
        severity: str,
        category: str,
        explanation: str,
        suggestion: str,
    ) -> None:
        findings.append(
            Finding(
                title=title,
                severity=severity,
                category=category,
                line_start=node.lineno,
                line_end=node.lineno,
                explanation=explanation,
                suggestion=suggestion,
                evidence=(ast.get_source_segment(code, node) or "对应 AST 节点")[:1500],
                rule_id=rule,
            )
        )

    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for default in [*node.args.defaults, *node.args.kw_defaults]:
                if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                    add(
                        default,
                        "AST002",
                        "可变默认参数会跨调用共享",
                        "medium",
                        "Bug",
                        "默认对象只在函数定义时创建一次；若函数或调用者修改该对象，后续省略该参数的调用会观察到先前状态。",
                        "默认值改为 None，并在函数内创建新的列表、字典或集合；若确实需要共享状态，请明确记录。",
                    )
        elif isinstance(node, ast.ExceptHandler):
            if node.type is None:
                add(
                    node,
                    "AST003",
                    "裸 except 捕获范围过宽",
                    "medium",
                    "异常处理",
                    "裸 except 也会捕获 KeyboardInterrupt 和 SystemExit，可能阻止用户中断或程序正常退出。",
                    "捕获明确的异常类型；需要兜底时优先使用 Exception，并保留必要的错误信息。",
                )
            if node.type is not None and all(isinstance(item, ast.Pass) for item in node.body):
                add(
                    node,
                    "AST004",
                    "异常被静默忽略",
                    "medium",
                    "异常处理",
                    "异常处理分支仅包含 pass；触发该异常后不会报告失败，可能掩盖数据或流程错误。需确认这是否为有意容错。",
                    "按业务语义记录、返回错误结果或重新抛出；若允许忽略，请记录原因。",
                )
        elif isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Div, ast.FloorDiv, ast.Mod)):
            if (
                isinstance(node.right, ast.Constant)
                and isinstance(node.right.value, (int, float))
                and node.right.value == 0
            ):
                add(
                    node,
                    "AST005",
                    "使用字面量零作为除数",
                    "high",
                    "Bug",
                    "如果执行到这个表达式且左侧是普通数值，将触发 ZeroDivisionError。",
                    "修正除数，并根据业务规则处理除数为零的情况。",
                )
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            if node.func.id in {"eval", "exec"}:
                add(
                    node,
                    "AST006",
                    "动态执行内容需要确认输入来源",
                    "high",
                    "安全性",
                    f"此处调用 {node.func.id}；若该名称指向内置函数且输入受外部控制，可能执行非预期 Python 代码。仅凭本文件不能确认输入可信度。",
                    "确认数据来源；解析简单字面量可考虑 ast.literal_eval，其他场景使用明确的解析器或受限表达式计算。",
                )
    return findings, "completed"


def ruff_checks(code: str) -> dict:
    """Use only fixed CLI options; the submission is passed on stdin as data."""
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "ruff",
                "check",
                "--isolated",
                "--no-cache",
                "--output-format",
                "json",
                "--select",
                "F,E9",
                "--stdin-filename",
                "review.py",
                "-",
            ],
            input=code,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=12,
            shell=False,
        )
        if result.returncode not in (0, 1):
            return {
                "status": "unavailable",
                "findings": [],
                "detail": "Ruff 不可用，请安装 requirements.txt 中的依赖。",
            }
        raw = json.loads(result.stdout)
        findings = []
        for item in raw:
            rule = item.get("code") or "syntax"
            row = min(max(1, item["location"]["row"]), len(code.splitlines()))
            findings.append(
                Finding(
                    title=f"Ruff {rule}: {item['message']}",
                    severity="high" if rule in {"F821", "F822", "F823", "syntax"} else "low",
                    category="Bug" if rule in {"F821", "F822", "F823", "syntax"} else "可维护性",
                    line_start=row,
                    line_end=row,
                    explanation=item["message"],
                    suggestion="检查对应变量、导入或表达式；结合调用上下文修正，并重新运行静态检查。",
                    evidence=code.splitlines()[row - 1][:1500] or "空行附近的解析诊断",
                    rule_id=f"RUFF:{rule}",
                )
            )
        retained, stats = select_findings(findings, 40)
        return {
            "status": "completed",
            "findings": [finding.model_dump() for finding in retained],
            **stats,
            "truncation_notice": selection_notice(stats, "Ruff"),
        }
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "findings": [], "detail": "Ruff 超过 12 秒，已终止该检查。"}
    except (OSError, ValueError, KeyError, TypeError):
        return {"status": "error", "findings": [], "detail": "无法读取 Ruff 的检查结果。"}


class ToolRegistry:
    def __init__(self, request: ReviewInput):
        self.request = request
        self.results: dict[str, Any] = {}
        self.checks: dict[str, str] = {}
        self.calculations: list[dict] = []

    def execute(self, name: str, arguments: str) -> dict:
        if name not in ARGUMENTS:
            return {"error": "未知工具。只允许提供的源码、静态检查和受限形状计算工具。"}
        try:
            args = ARGUMENTS[name].model_validate_json(arguments)
        except ValidationError:
            return {"error": "参数不符合工具 schema，请使用 JSON 对象和合法整数行号。"}
        calculators = {
            "calculate_slice": calculate_slice,
            "check_broadcast": check_broadcast,
            "keras_split": keras_split,
        }
        if name in calculators:
            result = calculators[name](args)
            self.calculations.append(
                {"tool": name, "arguments": args.model_dump(), "result": result}
            )
            return result
        if name == "check_semantics":
            if name not in self.results:
                self.results[name] = {
                    "facts": source_facts(self.request.code, self.request.language)
                }
            return self.results[name]
        if name == "read_source":
            lines = self.request.code.splitlines()
            end = args.end_line if args.end_line is not None else len(lines)
            if args.start_line > end or end > len(lines):
                return {"error": f"行号范围无效，本文件共有 {len(lines)} 行。"}
            result = {
                "filename": self.request.filename,
                "language": self.request.language,
                "total_lines": len(lines),
                "source": "\n".join(
                    f"{i + 1}: {lines[i]}" for i in range(args.start_line - 1, end)
                ),
            }
        elif name == "parse_structure" and self.request.language == "cpp":
            result = cpp_structure(self.request.code)
            self.checks["C++ 语法树与结构"] = result["status"]
        elif name == "parse_structure":
            tree, error = parse_tree(self.request.code)
            result = {"syntax_error": error, "symbols": [], "imports": []}
            if tree:
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                        if len(result["symbols"]) < 100:
                            result["symbols"].append(
                                {
                                    "kind": type(node).__name__,
                                    "name": node.name,
                                    "line": node.lineno,
                                    "end_line": node.end_lineno,
                                }
                            )
                    elif (
                        isinstance(node, (ast.Import, ast.ImportFrom))
                        and len(result["imports"]) < 100
                    ):
                        result["imports"].append(
                            {
                                "line": node.lineno,
                                "source": ast.get_source_segment(self.request.code, node),
                            }
                        )
            self.checks["Python 语法与结构"] = "syntax_error" if error else "completed"
        elif self.request.language == "cpp":
            if name in self.results:
                return self.results[name]
            result = cpp_checks(self.request.code)
            self.checks["C++ 本地规则"] = result["builtin_status"]
        else:
            if name in self.results:
                return self.results[name]
            findings, status = builtin_checks(self.request.code)
            ruff = ruff_checks(self.request.code)
            retained, builtin_stats = select_findings(findings, 40)
            # Each checker owns a bounded output. Count both channels so reports
            # can distinguish a tool cap from the later 30-candidate review cap.
            totals = {
                key: builtin_stats[key] + ruff.get(key, 0)
                for key in (
                    "total_count",
                    "unique_count",
                    "retained_count",
                    "omitted_count",
                    "duplicate_count",
                )
            }
            totals["truncated"] = bool(totals["omitted_count"])
            notices = [
                selection_notice(builtin_stats, "Python AST"),
                ruff.get("truncation_notice", ""),
            ]
            result = {
                "builtin_status": status,
                "builtin_findings": [f.model_dump() for f in retained],
                "builtin_selection": builtin_stats,
                "ruff": ruff,
                **totals,
                "truncation_notice": " ".join(notice for notice in notices if notice),
            }
            self.checks["内置 AST 规则"] = status
            self.checks["Ruff 静态检查"] = ruff["status"]
        self.results[name] = result
        return result


def compact_tool_result(name: str, result: dict) -> dict:
    """Keep one source snapshot in the prompt; tool results reference its lines."""
    if "error" in result:
        return result
    if name == "read_source":
        return {key: value for key, value in result.items() if key != "source"} | {
            "source_reference": "本请求唯一的 numbered_source；按原行号读取。"
        }
    if name == "check_semantics":
        return {"fact_ids": [fact["id"] for fact in result["facts"]], "reference": "semantic_facts"}
    if name == "run_static_checks":

        def compact(items):
            return [
                {
                    key: value
                    for key, value in item.items()
                    if key
                    in {"rule_id", "line_start", "line_end", "severity", "title", "explanation"}
                }
                for item in items
            ]

        output = dict(result)
        output["builtin_findings"] = compact(result.get("builtin_findings", []))
        if "ruff" in result:
            output["ruff"] = {
                **result["ruff"],
                "findings": compact(result["ruff"].get("findings", [])),
            }
        return output
    return result
