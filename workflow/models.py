"""Strict role contracts and immutable, source-bound artifacts."""

import ast
import hashlib
import io
import re
import tokenize
from typing import TypedDict

from pydantic import BaseModel, ConfigDict, Field

from review_agent.models import ReviewInput


class WorkflowState(TypedDict):
    data: dict


class CodingOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=1)
    summary: str = Field(default="", max_length=2000)


class FixOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_code_hash: str
    revised_code: str = Field(min_length=1)
    addressed_finding_ids: list[str] = Field(default_factory=list, max_length=80)
    change_summary: str = Field(default="", max_length=2000)
    unresolved_items: list[str] = Field(default_factory=list, max_length=30)


class RequirementOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: str = Field(pattern=r"^(met|gap|unknown)$")
    reason: str = Field(min_length=1, max_length=1500)


def source_hash(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()


def requires_measured_evidence(task: str) -> bool:
    """Conservatively identify goals which static source cannot establish."""
    objective = re.search(r"提升|提高|改善|优化|降低|减少|增加|improv|increas|reduc|optimi", task, re.I)
    metric = re.search(r"准确|精度|识别率|召回|性能|速度|延迟|内存|吞吐|accuracy|precision|recall|performance|latency|memory|speed|throughput", task, re.I)
    return bool(objective and metric) or bool(re.search(r"更快|更准确", task))


def meaningful_source_change(before: str, after: str) -> bool:
    """Comments/formatting alone cannot resolve a confirmed functional defect."""
    if before == after:
        return False
    class WithoutDocstrings(ast.NodeTransformer):
        def visit(self, node):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                if node.body and isinstance(node.body[0], ast.Expr):
                    value = node.body[0].value
                    if isinstance(value, ast.Constant) and isinstance(value.value, str):
                        node.body = node.body[1:]
            return super().visit(node)
    try:
        old_tree, new_tree = [WithoutDocstrings().visit(ast.parse(source)) for source in (before, after)]
        return ast.dump(old_tree, include_attributes=False) != ast.dump(new_tree, include_attributes=False)
    except (SyntaxError, ValueError, RecursionError):
        def significant(source):
            ignored = {tokenize.COMMENT, tokenize.NL, tokenize.NEWLINE, tokenize.ENDMARKER}
            return [(item.type, item.string) for item in tokenize.generate_tokens(io.StringIO(source).readline)
                    if item.type not in ignored]
        try:
            return significant(before) != significant(after)
        except (tokenize.TokenError, IndentationError, SyntaxError):
            pass
        return before.strip() != after.strip()


def normalize_source(code: str, filename: str) -> str:
    request = ReviewInput(code=code, filename=filename)
    if request.language != "python":
        raise ValueError("自动修复工作流仅支持 Python；C++ 请使用原代码审查入口。")
    if code.lstrip().startswith("```"):
        raise ValueError("需要完整源码字段，不接受 Markdown 代码块。")
    if re.search(r"(?im)^\s*(?:\.\.\.|#\s*(?:remaining code|rest unchanged|其余代码省略|其他代码不变))\s*$", code):
        raise ValueError("完整源码中不能包含省略占位。")
    return request.code


def artifact(code: str, filename: str, index: int, parent: str | None, origin: str) -> dict:
    normalized = normalize_source(code, filename)
    digest = source_hash(normalized)
    return {"id": f"v{index}-{digest[:12]}", "label": f"v{index}", "code": normalized,
            "hash": digest, "parent": parent, "origin": origin}


def current_artifact(data: dict) -> dict:
    return next(item for item in data["versions"] if item["id"] == data["current_version"])


def public_interfaces(code: str) -> dict:
    """A narrow deletion/signature guard; this does not prove behavior preservation."""
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError):
        return {}
    result = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            args = node.args
            result[node.name] = {"kind": type(node).__name__, "positional": [a.arg for a in args.posonlyargs + args.args],
                                 "posonly": len(args.posonlyargs), "keyword": [a.arg for a in args.kwonlyargs],
                                 "vararg": args.vararg.arg if args.vararg else None,
                                 "kwarg": args.kwarg.arg if args.kwarg else None}
        elif isinstance(node, ast.ClassDef) and not node.name.startswith("_"):
            result[node.name] = {"kind": "ClassDef"}
    return result
