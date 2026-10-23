"""C++ syntax trees and conservative local rules; no compilation or header loading."""

import re
import warnings
from functools import lru_cache
from typing import Any

from review_agent.models import Finding
from review_agent.selection import select_findings, selection_notice

CPP_LIMITATIONS = [
    "C++ 使用 Tree-sitter 解析当前文件；不展开宏、不读取 #include 头文件，不进行编译、链接或运行。",
    "C++ 本地规则不做完整类型推导、控制流、跨文件或生命周期分析；模板、宏及条件编译可能影响解析和判断。",
]


@lru_cache(maxsize=1)
def cpp_language():
    import tree_sitter_cpp
    from tree_sitter import Language

    return Language(tree_sitter_cpp.language())


def parse_cpp(code: str) -> tuple[Any, str, str]:
    try:
        from tree_sitter import Parser

        # The 0.25.2 Windows wheel crashes with progress callbacks in a Streamlit
        # worker thread. Keep its supported native timeout with a bounded input.
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore", message="Use the progress_callback in parse", category=DeprecationWarning
            )
            parser = Parser(cpp_language(), timeout_micros=500_000)
        tree = parser.parse(code.encode("utf-8"))
        if tree is None:
            return None, "timeout", "C++ 解析超过时间预算，未完成检查。"
        return tree, "syntax_error" if tree.root_node.has_error else "completed", ""
    except (ImportError, OSError, TypeError):
        return None, "unavailable", "C++ 解析器不可用，请安装 requirements.txt 中的依赖。"
    except (ValueError, MemoryError):
        return None, "error", "C++ 解析失败或达到解析预算，请缩小输入范围。"


def walk(node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.children))


def text(node, source: bytes) -> str:
    return source[node.start_byte : node.end_byte].decode("utf-8") if node is not None else ""


def declarator_name(node, source: bytes) -> str:
    while node is not None:
        child = node.child_by_field_name("declarator")
        if child is None:
            return text(node, source)
        node = child
    return ""


def cpp_structure(code: str) -> dict:
    tree, status, detail = parse_cpp(code)
    result = {
        "language": "cpp",
        "status": status,
        "detail": detail,
        "symbols": [],
        "imports": [],
        "parse_errors": [],
        "limitations": CPP_LIMITATIONS,
    }
    if tree is None:
        return result
    source = code.encode("utf-8")
    for node in walk(tree.root_node):
        if (node.type == "ERROR" or node.is_missing) and len(result["parse_errors"]) < 10:
            result["parse_errors"].append(
                {
                    "line": node.start_point.row + 1,
                    "detail": f"缺少 {node.type}" if node.is_missing else "无法解析的语法片段",
                }
            )
        if len(result["symbols"]) < 100:
            if node.type == "function_definition":
                result["symbols"].append(
                    {
                        "kind": "function",
                        "name": declarator_name(node.child_by_field_name("declarator"), source),
                        "line": node.start_point.row + 1,
                        "end_line": node.end_point.row + 1,
                    }
                )
            elif node.type in {"class_specifier", "struct_specifier", "namespace_definition"}:
                result["symbols"].append(
                    {
                        "kind": node.type,
                        "name": text(node.child_by_field_name("name"), source),
                        "line": node.start_point.row + 1,
                        "end_line": node.end_point.row + 1,
                    }
                )
        if node.type == "preproc_include" and len(result["imports"]) < 100:
            result["imports"].append(
                {"line": node.start_point.row + 1, "source": text(node, source)[:500]}
            )
    return result


def cpp_checks(code: str) -> dict:
    tree, status, detail = parse_cpp(code)
    result = {
        "language": "cpp",
        "builtin_status": status,
        "builtin_findings": [],
        "detail": detail,
        "limitations": CPP_LIMITATIONS,
        "total_count": 0,
        "unique_count": 0,
        "retained_count": 0,
        "omitted_count": 0,
        "duplicate_count": 0,
        "truncated": False,
        "truncation_notice": "",
    }
    if tree is None:
        return result
    source = code.encode("utf-8")
    line_count = len(code.splitlines())
    findings = []

    def add(node, rule, title, severity, category, explanation, suggestion):
        line = min(node.start_point.row + 1, line_count)
        findings.append(
            Finding(
                title=title,
                severity=severity,
                category=category,
                line_start=line,
                line_end=line,
                explanation=explanation,
                suggestion=suggestion,
                evidence=text(node, source)[:1500] or f"第 {line} 行附近缺少 {node.type}",
                rule_id=rule,
            )
        )

    def select_result(limit: int) -> dict:
        retained, stats = select_findings(findings, limit)
        result.update(stats)
        result["builtin_findings"] = [finding.model_dump() for finding in retained]
        result["truncation_notice"] = selection_notice(stats, "C++ 本地规则")
        return result

    if tree.root_node.has_error:
        for node in walk(tree.root_node):
            if node.type == "ERROR" or node.is_missing:
                add(
                    node,
                    "CPP001",
                    "C++ 语法解析发现不完整或无法识别的片段",
                    "medium",
                    "Bug",
                    "Tree-sitter 无法完整解析该位置；可能是语法缺失，也可能依赖未展开的宏或上下文。这不是编译器诊断。",
                    "检查括号、分号和声明；在完整项目的实际编译配置下确认，并补充相关宏或头文件上下文。",
                )
        return select_result(5)

    for node in walk(tree.root_node):
        if node.type == "binary_expression":
            right = node.child_by_field_name("right")
            while (
                right is not None
                and right.type == "parenthesized_expression"
                and len(right.named_children) == 1
            ):
                right = right.named_children[0]
            operator = text(node.child_by_field_name("operator"), source)
            literal = text(right, source).replace("'", "")
            left = node.child_by_field_name("left")
            left_literal = text(left, source).lower()
            known_float = (
                left is not None
                and left.type == "number_literal"
                and (
                    "." in left_literal
                    or "p" in left_literal
                    or ("e" in left_literal and not left_literal.startswith("0x"))
                )
            )
            if (
                operator in {"/", "%"}
                and right is not None
                and right.type == "number_literal"
                and re.fullmatch(r"(?:0+|0x0+|0b0+)(?:u(?:ll?|z)?|(?:ll?|z)u?)?", literal, re.I)
                and not known_float
            ):
                add(
                    node,
                    "CPP002",
                    "零除数存在整数运算风险",
                    "high",
                    "Bug",
                    "右操作数是整型字面量零。若这里使用内置整数除法或取模，执行到该表达式会产生未定义行为；本地工具未推导左操作数类型。",
                    "修正除数并检查零值分支；若使用浮点或自定义运算符，请结合具体类型确认行为。",
                )
        elif node.type == "call_expression":
            function = text(node.child_by_field_name("function"), source)
            if function in {
                prefix + name
                for prefix in ("", "::", "std::")
                for name in ("gets", "strcpy", "strcat", "sprintf")
            }:
                add(
                    node,
                    "CPP003",
                    "字符串写入缺少目标容量参数",
                    "medium",
                    "安全性",
                    f"此处调用 {function}。若它是同名 C 标准库函数且结果超出目标缓冲区容量，可能发生越界写入；仍需确认目标容量与输入长度。",
                    "优先使用 std::string；格式化到字符数组时传入真实容量并检查 snprintf 返回值，避免截断或越界。",
                )
        elif node.type in {"if_statement", "while_statement", "do_statement"}:
            condition = node.child_by_field_name("condition")
            value = condition.child_by_field_name("value") if condition is not None else None
            if value is not None and value.type == "assignment_expression":
                add(
                    value,
                    "CPP004",
                    "条件表达式中直接使用赋值",
                    "low",
                    "Bug",
                    "条件会先修改左值，再根据赋值结果判断真假。若原意是比较，相应分支行为将错误；也可能是有意写法。",
                    "确认是否应使用 ==；有意赋值时考虑拆分赋值与判断，或用额外括号明确意图。",
                )
        elif node.type == "catch_clause":
            body = node.child_by_field_name("body")
            if body is not None and not any(
                child.type != "comment" for child in body.named_children
            ):
                add(
                    node,
                    "CPP005",
                    "catch 分支静默忽略异常",
                    "medium",
                    "异常处理",
                    "捕获异常后没有恢复、记录或向调用者报告；可能掩盖操作失败，需确认是否是有意容错。",
                    "按业务语义处理失败、记录错误或重新抛出；有意忽略时记录原因。",
                )
        elif node.type == "compound_statement":
            statements = [item for item in node.named_children if item.type != "comment"]
            for allocation, release in zip(statements, statements[1:]):
                if allocation.type != "declaration" or release.type != "expression_statement":
                    continue
                initializers = [
                    child for child in allocation.named_children if child.type == "init_declarator"
                ]
                if len(initializers) != 1 or len(release.named_children) != 1:
                    continue
                initializer, deletion = initializers[0], release.named_children[0]
                value = initializer.child_by_field_name("value")
                if (
                    value is None
                    or value.type != "new_expression"
                    or deletion.type != "delete_expression"
                ):
                    continue
                # Adjacent statements only: do not guess through reassignment, aliases or branches.
                name = declarator_name(initializer.child_by_field_name("declarator"), source)
                if (
                    len(deletion.named_children) != 1
                    or deletion.named_children[0].type != "identifier"
                ):
                    continue
                if text(deletion.named_children[0], source) != name:
                    continue
                array_new = any(child.type == "new_declarator" for child in value.named_children)
                array_delete = any(child.type == "[" for child in deletion.children)
                if array_new != array_delete:
                    add(
                        deletion,
                        "CPP006",
                        "new 与 delete 的数组形式不匹配",
                        "high",
                        "Bug",
                        "相邻语句创建和释放同一指针，但 new[] / delete[] 的数组形式不一致，可能产生未定义行为。",
                        "new[] 配对 delete[]，单对象 new 配对 delete；优先使用 std::vector 或适当类型的智能指针。",
                    )
    return select_result(40)
