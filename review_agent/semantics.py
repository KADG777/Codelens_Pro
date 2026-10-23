"""Bounded numeric tools and source-bound facts. No eval, imports or user execution."""

import ast
import math
import re
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

KERAS_URL = "https://keras.io/api/models/model_training_apis/#fit-method"
KERAS_CONTRACT = (
    "Keras Model.fit 对 NumPy/张量输入使用 validation_split 时，先取末尾样本作验证，再打乱训练部分；"
    "验证部分不用于本次 fit 的训练。validation_data 非空时覆盖 validation_split。"
    "同源不等于样本重叠或泄漏，也不能单凭同源断言指标虚高；独立测试集是另一个评估问题。"
)
Bound = Annotated[int, Field(ge=-1_000_000, le=1_000_000, strict=True)]
Dimension = Annotated[int, Field(ge=0, le=1_000_000, strict=True)]


class ClosedArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AxisSlice(ClosedArguments):
    start: Bound | None = None
    stop: Bound | None = None
    step: Bound | None = None


class SliceArguments(ClosedArguments):
    shape: list[Dimension] = Field(min_length=1, max_length=4)
    slices: list[AxisSlice] = Field(min_length=1, max_length=4)


class BroadcastArguments(ClosedArguments):
    target_shape: list[Dimension] = Field(min_length=1, max_length=4)
    source_shape: list[Dimension] = Field(min_length=1, max_length=4)


class SplitArguments(ClosedArguments):
    samples: int = Field(ge=2, le=1_000_000, strict=True)
    validation_split: float = Field(gt=0, lt=1, allow_inf_nan=False)


def calculate_slice(args: SliceArguments) -> dict:
    if len(args.slices) > len(args.shape):
        return {"error": "切片轴数不能超过数组维数。"}
    normalized, shape = [], []
    for size, axis in zip(args.shape, args.slices):
        if axis.step == 0:
            return {"error": "切片步长不能为零。"}
        bounds = slice(axis.start, axis.stop, axis.step).indices(size)
        normalized.append(list(bounds))
        shape.append(len(range(*bounds)))
    shape.extend(args.shape[len(args.slices) :])
    return {
        "shape": shape,
        "empty": 0 in shape,
        "normalized_slices": normalized,
        "scope": "给定整数形状与基本切片的计算，不验证模型提供的参数是否对应源码。",
    }


def check_broadcast(args: BroadcastArguments) -> dict:
    # NumPy assignment ignores leading unit dimensions on the RHS.
    source = list(args.source_shape)
    while len(source) > len(args.target_shape) and source[0] == 1:
        source.pop(0)
    compatible = len(source) <= len(args.target_shape) and all(
        src == 1 or src == dst for src, dst in zip(reversed(source), reversed(args.target_shape))
    )
    return {
        "compatible": compatible,
        "target_shape": args.target_shape,
        "source_shape": args.source_shape,
        "scope": "仅计算基本 NumPy 赋值广播形状，不执行数组操作。",
    }


def keras_split(args: SplitArguments) -> dict:
    boundary = math.floor(args.samples * (1 - args.validation_split))
    if boundary in (0, args.samples):
        return {"error": "该样本数与比例无法同时产生非空训练集和验证集。"}
    return {
        "train_range": [0, boundary],
        "validation_range": [boundary, args.samples],
        "range_convention": "左闭右开",
        "overlap": False,
        "random_split": False,
        "contract": KERAS_CONTRACT,
        "source_url": KERAS_URL,
        "scope": "仅在接收者为 Keras Model、输入为 NumPy/张量且未覆盖 validation_data 时适用。",
    }


def _expr(node):
    try:
        return ast.unparse(node) if node is not None else ""
    except (RecursionError, MemoryError):
        return ""


def _name(node):
    return node.id if isinstance(node, ast.Name) else None


def _assigned_names(node):
    return {
        item.id
        for item in ast.walk(node)
        if isinstance(item, ast.Name) and isinstance(item.ctx, ast.Store)
    }


def _constant(node):
    if isinstance(node, ast.Constant) and type(node.value) in (int, float):
        return node.value
    return None


def source_facts(code: str, language: str) -> list[dict]:
    """Recognize only narrow AST patterns; unsupported shapes are left unknown."""
    if language != "python":
        return []
    try:
        tree = ast.parse(code)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return []
    facts = []
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            aliases.update(
                {
                    item.asname or item.name.split(".")[0]: item.name
                    if item.asname
                    else item.name.split(".")[0]
                    for item in node.names
                }
            )
        elif isinstance(node, ast.ImportFrom) and node.module:
            aliases.update(
                {item.asname or item.name: f"{node.module}.{item.name}" for item in node.names}
            )
    # Reject shadowed imports rather than assigning a library contract to a custom API.
    rebound = _assigned_names(tree) | {
        node.arg for node in ast.walk(tree) if isinstance(node, ast.arg)
    }
    rebound |= {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    aliases = {key: value for key, value in aliases.items() if key not in rebound}

    def resolve(node):
        value = _expr(node)
        head, _, tail = value.partition(".")
        return aliases.get(head, "?") + ("." + tail if tail else "")

    for parent in ast.walk(tree):
        body = getattr(parent, "body", None)
        if not isinstance(body, list):
            continue
        keras_receivers = set()
        for statement in body:
            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                continue
            keras_receivers -= _assigned_names(statement)
            if isinstance(statement, ast.Assign) and isinstance(statement.value, ast.Call):
                if resolve(statement.value.func) in {
                    f"{prefix}.{kind}"
                    for prefix in (
                        "keras",
                        "keras.models",
                        "tensorflow.keras",
                        "tensorflow.keras.models",
                    )
                    for kind in ("Sequential", "Model")
                }:
                    keras_receivers.update(
                        _name(target) for target in statement.targets if _name(target)
                    )
            # Calls are inspected in their own statement, never inside another scope/control body.
            if not isinstance(statement, (ast.Expr, ast.Assign, ast.Return)):
                continue
            for node in ast.walk(statement):
                if not (
                    isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "fit"
                    and _name(node.func.value) in keras_receivers
                ):
                    continue
                keywords = {item.arg: item.value for item in node.keywords}
                fraction = _constant(keywords.get("validation_split"))
                if fraction is None or not 0 < fraction < 1 or None in keywords:
                    continue
                override = keywords.get("validation_data")
                active = (
                    override is None
                    or isinstance(override, ast.Constant)
                    and override.value is None
                )
                facts.append(
                    {
                        "kind": "keras_validation_split",
                        "line_start": node.lineno,
                        "line_end": node.end_lineno,
                        "active": active,
                        "fraction": fraction,
                        "statement": KERAS_CONTRACT,
                        "source_url": KERAS_URL,
                        "scope": "API 合同知识，假设标准 Keras 方法未被替换；不证明数据无重复、预处理无泄漏或没有额外训练。",
                    }
                )
        for index, statement in enumerate(body):
            # h,w = frame.shape[:2]; box = positive literal; x,y = ...; x2,y2 = ...
            if not (
                isinstance(statement, ast.Assign)
                and len(statement.targets) == 1
                and isinstance(statement.targets[0], ast.Tuple)
                and len(statement.targets[0].elts) == 2
            ):
                continue
            h, w = (_name(item) for item in statement.targets[0].elts)
            value = statement.value
            if not (
                h
                and w
                and isinstance(value, ast.Subscript)
                and isinstance(value.value, ast.Attribute)
                and value.value.attr == "shape"
                and isinstance(value.slice, ast.Slice)
                and value.slice.lower is None
                and _constant(value.slice.upper) == 2
                and value.slice.step is None
            ):
                continue
            frame = _name(value.value.value)
            window = body[index + 1 : index + 4]
            if (
                not frame
                or len(window) != 3
                or not all(isinstance(item, ast.Assign) for item in window)
            ):
                continue
            box_assign, origin, end = window
            if any(len(item.targets) != 1 for item in window):
                continue
            box = _name(box_assign.targets[0])
            size = _constant(box_assign.value)
            if not box or type(size) is not int or not 1 <= size <= 1_000_000:
                continue
            try:
                x, y = (_name(item) for item in origin.targets[0].elts)
                x2, y2 = (_name(item) for item in end.targets[0].elts)
            except (AttributeError, ValueError):
                continue
            if (
                not all((x, y, x2, y2))
                or _expr(origin.value) != f"(({w} - {box}) // 2, ({h} - {box}) // 2)"
                or _expr(end.value) != f"({x} + {box}, {y} + {box})"
            ):
                continue
            names = {frame, h, w, box, x, y, x2, y2}
            if len(names) != 8:
                continue
            for later in body[index + 4 : index + 8]:
                if (
                    isinstance(later, ast.Assign)
                    and _expr(later.value) == f"{frame}[{y}:{y2}, {x}:{x2}]"
                ):
                    coords = [(120 - size) // 2, (160 - size) // 2]
                    sample = calculate_slice(
                        SliceArguments(
                            shape=[120, 160, 3],
                            slices=[AxisSlice(start=start, stop=start + size) for start in coords],
                        )
                    )
                    facts.append(
                        {
                            "kind": "centered_roi",
                            "line_start": statement.lineno,
                            "line_end": later.end_lineno,
                            "box_size": size,
                            "sample_frame_shape": [120, 160, 3],
                            "sample": sample,
                            "sample_trace": (
                                f"frame.shape=(120,160,3), box_size={size} 时，"
                                f"原 y/x 切片起点为 {coords}，终点为 {[c + size for c in coords]}；"
                                f"标准化切片为 {sample['normalized_slices']}，输出形状为 {sample['shape']}。"
                                "负索引先加该轴长度，再裁剪到合法范围，不能直接把负起点改为 0。"
                            ),
                            "statement": "对正尺寸 NumPy 数组和正 box_size，此固定中心公式的基本切片非空；负起点可能使区域偏离预期中心。",
                            "scope": "依赖 NumPy 基本切片语义；不涵盖零尺寸、非数组对象或其他预处理错误。",
                        }
                    )
                    break
                # Permit the common drawing call; do not infer through arbitrary mutation/calls.
                drawing = (
                    isinstance(later, ast.Expr)
                    and isinstance(later.value, ast.Call)
                    and resolve(later.value.func) == "cv2.rectangle"
                )
                if _assigned_names(later) & names or not drawing:
                    break
    return [{"id": f"fact_{index}", **fact} for index, fact in enumerate(facts[:20], 1)]


def text_conflicts(text: str, facts: list[dict], *, roi_bound: bool = False) -> list[str]:
    """Conservative checks of known contradictions, not a general prose verifier."""
    messages = []
    clauses = re.split(r"[。！？!?；;\n]|，?但是|，?然而|，?不过", text)
    for fact in facts:
        for clause in clauses:
            if not clause.strip():
                continue
            if fact["kind"] == "centered_roi":
                roi = roi_bound or re.search(r"ROI|中心切片|中心裁剪", clause, re.I)
                empty = re.search(
                    r"为空|空(?:数组|切片|区域|ROI|图像)|empty|0\s*行|零行", clause, re.I
                )
                crash = re.search(
                    r"低分辨率|小于\s*200|160\s*[×x*]\s*120", clause, re.I
                ) and re.search(r"崩溃|抛异常|报错", clause)
                negated = re.search(
                    r"非空|不为空|不是空|不会.{0,6}(?:空|崩溃)|不能.{0,10}(?:断定|认定|推断)|没有证据|未证明|尚未证实|不成立|已否决|误报|错误说法|not empty|nonempty|empty\s*[=:]\s*false",
                    clause,
                    re.I,
                )
                outside = re.search(
                    r"零尺寸|零大小|(?:宽|高).{0,3}(?:为零|为 ?0)|zero.size", clause, re.I
                )
                if roi and (empty or crash) and not negated and not outside:
                    messages.append(
                        f"{fact['id']}：正尺寸中心切片的空 ROI 断言与源码计算冲突；算例为 {fact['sample']['shape']}、empty=False。"
                    )
            elif fact["kind"] == "keras_validation_split" and fact["active"]:
                topic = re.search(r"验证|训练|validation|泄漏", clause, re.I)
                random = re.search(
                    r"随机.{0,8}(?:取|切分|划分|分配)|(?:切分|划分).{0,8}随机", clause
                )
                leakage = re.search(
                    r"同源.{0,18}(?:就|所以|因此|说明|证明|意味着|导致|造成|存在|属于).{0,12}(?:泄漏|高估|虚高)",
                    clause,
                )
                negated = re.search(
                    r"不是随机|非随机|不随机|不会随机|不等于|不能|不应|不意味|并不|不构成|不.{0,5}(?:证明|说明|认定)|没有证据|不足以|不成立|误报|已否决",
                    clause,
                )
                alternative = re.search(
                    r"(?:若|如果|希望|想).{0,6}随机(?:切分|划分)|train_test_split", clause
                )
                if topic and (random or leakage) and not negated and not alternative:
                    messages.append(
                        f"{fact['id']}：Keras validation_split 先取末尾验证样本，验证部分不参与该次训练；同源不足以证明泄漏或指标虚高。"
                    )
    return list(dict.fromkeys(messages))


def conflicting_fact(finding, facts: list[dict], extra: str = "") -> str:
    """Quarantine contradicted claims; never use a model-chosen calculation as proof."""
    applicable = [
        fact
        for fact in facts
        if fact["kind"] != "centered_roi"
        or (finding.line_start <= fact["line_end"] and finding.line_end >= fact["line_start"])
    ]
    prefix = "ROI：" if any(f["kind"] == "centered_roi" for f in applicable) else ""
    claim = "。".join(prefix + part for part in (finding.title, finding.explanation, extra) if part)
    return " ".join(text_conflicts(claim, applicable, roi_bound=True))
