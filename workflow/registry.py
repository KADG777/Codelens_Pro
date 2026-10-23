"""Explicit public task contracts and separate, evaluator-only holdout cases.

Only model_contract() enters graph state or model payloads. public_metadata()
also exposes labelled reference fixtures for the UI and evaluation controls.
Holdout cases are consumed after a workflow ends, never by any Agent.
"""

import copy
import hashlib
import json
from dataclasses import dataclass

from workflow.examples import BUGGY_ORDERS, CORRECT_ORDERS, ORDER_HARNESS, ORDER_TASK


@dataclass(frozen=True)
class FunctionCase:
    id: str
    args: tuple
    expected: object = None
    raises: str | None = None
    preserve_input: bool = True

    def to_dict(self) -> dict:
        return copy.deepcopy({"id": self.id, "args": list(self.args), "expected": self.expected,
                              "raises": self.raises, "preserve_input": self.preserve_input})


@dataclass(frozen=True)
class TaskSpec:
    id: str
    label: str
    task: str
    entrypoint: str
    parameters: tuple[str, ...]
    public_cases: tuple[FunctionCase, ...]
    holdout_cases: tuple[FunctionCase, ...]
    reference_code: str
    buggy_code: str = ""
    filename: str = "solution.py"
    demo_available: bool = False

    def public_metadata(self) -> dict:
        return {"id": self.id, "acceptance_id": self.id, "label": self.label, "task": self.task,
                "entrypoint": self.entrypoint, "parameters": list(self.parameters), "filename": self.filename,
                "demo_available": self.demo_available, "buggy_code": self.buggy_code,
                "correct_code": self.reference_code}

    def model_contract(self) -> dict:
        return {key: value for key, value in self.public_metadata().items()
                if key in {"id", "label", "task", "entrypoint", "parameters"}}


C = FunctionCase
TEXT_TASK = ("实现 text_stats(text)，输入为字符串，返回字典 characters、words、lines。"
             "characters 为全部字符数（包含空白），words 按 str.split() 统计非空白词，"
             "lines 按 str.splitlines() 统计行数；空字符串三个值均为 0。只用标准库。")
TEXT_CORRECT = ('def text_stats(text):\n'
                '    return {"characters": len(text), "words": len(text.split()), "lines": len(text.splitlines())}\n')
TEXT_BUGGY = ('def text_stats(text):\n'
              '    return {"characters": len(text), "words": word_count, "lines": len(text.splitlines())}\n')

_TASKS: dict[str, TaskSpec] = {}


def register_task_spec(spec: TaskSpec, *, replace: bool = False) -> None:
    if not spec.id or not spec.entrypoint.isidentifier() or not spec.public_cases:
        raise ValueError("任务注册需要 ID、合法入口函数和至少一个公开验收用例。")
    if spec.id in _TASKS and not replace:
        raise ValueError(f"任务已注册：{spec.id}")
    for cases in (spec.public_cases, spec.holdout_cases):
        if len({case.id for case in cases}) != len(cases):
            raise ValueError("同一验收集合中的用例 ID 不能重复。")
    _TASKS[spec.id] = spec


def get_task_spec(acceptance_id: str) -> TaskSpec:
    try:
        return _TASKS[acceptance_id]
    except KeyError:
        raise ValueError(f"未注册的验收合同：{acceptance_id}") from None


def list_task_specs() -> list[dict]:
    return [spec.public_metadata() for spec in _TASKS.values()]


def build_harness(acceptance_id: str, *, holdout: bool = False) -> str:
    spec = get_task_spec(acceptance_id)
    if acceptance_id == "orders-v1" and not holdout:
        return ORDER_HARNESS  # Preserve the exact schema-1 historical acceptance hash.
    selected = spec.holdout_cases if holdout else spec.public_cases
    payload = json.dumps([case.to_dict() for case in selected], ensure_ascii=False)
    return f'''import builtins
import copy
import json
import runpy

cases = json.loads({payload!r})
checks = []
try:
    namespace = runpy.run_path("/workspace/solution.py")
    function = namespace[{spec.entrypoint!r}]
    for case in cases:
        arguments = copy.deepcopy(case["args"])
        before = copy.deepcopy(arguments)
        try:
            actual = function(*arguments)
            valid = case["raises"] is None and actual == case["expected"]
            detail = repr(actual)[:300]
        except Exception as exc:
            expected_type = getattr(builtins, case["raises"], None) if case["raises"] else None
            valid = expected_type is not None and isinstance(exc, expected_type)
            detail = type(exc).__name__
        if case["preserve_input"] and arguments != before:
            valid = False
            detail += "; input modified"
        checks.append({{"id": case["id"], "name": case["id"], "status": "passed" if valid else "failed", "detail": detail, "expected": repr(case["expected"]) if not case["raises"] else case["raises"]}})
except Exception as exc:
    checks.append({{"id": "load", "name": "load", "status": "failed", "detail": type(exc).__name__}})
print("CODELENS_RESULT=" + json.dumps(checks, ensure_ascii=False))
'''


def get_acceptance_hash(acceptance_id: str) -> str:
    harness = "none" if acceptance_id == "none" else build_harness(acceptance_id)
    return hashlib.sha256(harness.encode("utf-8")).hexdigest()


register_task_spec(TaskSpec("orders-v1", "订单统计", ORDER_TASK, "summarize_orders", ("orders",),
    (C("empty", ([],), {"paid_count": 0, "total_cents": 0}),
     C("mixed", ([{"amount_cents": 120, "paid": True}, {"amount_cents": 90, "paid": False}],), {"paid_count": 1, "total_cents": 120}),
     C("zero", ([{"amount_cents": 0, "paid": True}],), {"paid_count": 1, "total_cents": 0}),
     C("missing", ([{"paid": True}],), raises="ValueError"),
     C("negative", ([{"amount_cents": -1, "paid": False}],), raises="ValueError"),
     C("bool_amount", ([{"amount_cents": True, "paid": True}],), raises="ValueError"),
     C("bool_paid", ([{"amount_cents": 20, "paid": 1}],), raises="ValueError"),
     C("bad_order", ([None],), raises="ValueError")),
    (C("unpaid_validation", ([{"amount_cents": "5", "paid": False}],), raises="ValueError"),
     C("multiple_paid", ([{"amount_cents": 3, "paid": True}, {"amount_cents": 7, "paid": True}],), {"paid_count": 2, "total_cents": 10})),
    CORRECT_ORDERS, BUGGY_ORDERS, demo_available=True))

register_task_spec(TaskSpec("text-stats-v1", "文本统计", TEXT_TASK, "text_stats", ("text",),
    (C("empty", ("",), {"characters": 0, "words": 0, "lines": 0}),
     C("words", ("one two",), {"characters": 7, "words": 2, "lines": 1}),
     C("multiline", ("a\nb\n",), {"characters": 4, "words": 2, "lines": 2})),
    (C("spaces", (" \t ",), {"characters": 3, "words": 0, "lines": 1}),
     C("unicode", ("你好\nworld",), {"characters": 8, "words": 2, "lines": 2})),
    TEXT_CORRECT, TEXT_BUGGY, demo_available=True))

register_task_spec(TaskSpec("dedupe-v1", "稳定去重", "实现 unique_in_order(items)：输入整数列表，返回首次出现顺序的去重列表，不修改输入。", "unique_in_order", ("items",),
    (C("empty", ([],), []), C("duplicates", ([2, 1, 2, 3, 1],), [2, 1, 3])),
    (C("negative", ([-1, 0, -1, 0, 2],), [-1, 0, 2]), C("single", ([4, 4, 4],), [4]),
     C("unsorted_first_seen", ([3, 1, 3, 2],), [3, 1, 2])),
    "def unique_in_order(items):\n    return list(dict.fromkeys(items))\n"))

register_task_spec(TaskSpec("clamp-v1", "数值限幅", "实现 clamp(value, lower, upper)：整数上下限内返回 value，越界返回对应边界；lower>upper 时抛 ValueError。", "clamp", ("value", "lower", "upper"),
    (C("inside", (3, 0, 5), 3), C("below", (-2, 0, 5), 0), C("bad_range", (2, 5, 0), raises="ValueError")),
    (C("above", (9, -3, 4), 4), C("equal", (9, 2, 2), 2)),
    "def clamp(value, lower, upper):\n    if lower > upper:\n        raise ValueError('invalid bounds')\n    return min(max(value, lower), upper)\n"))

register_task_spec(TaskSpec("run-length-v1", "游程编码", "实现 run_length_encode(text)：按连续相同字符返回 [[字符, 次数], ...]，区分大小写，空串返回空列表。", "run_length_encode", ("text",),
    (C("empty", ("",), []), C("runs", ("aaabb",), [["a", 3], ["b", 2]])),
    (C("separated", ("aba",), [["a", 1], ["b", 1], ["a", 1]]), C("unicode", ("中中A",), [["中", 2], ["A", 1]])),
    "def run_length_encode(text):\n    result = []\n    for char in text:\n        if result and result[-1][0] == char:\n            result[-1][1] += 1\n        else:\n            result.append([char, 1])\n    return result\n"))

register_task_spec(TaskSpec("merge-intervals-v1", "区间合并", "实现 merge_intervals(intervals)：输入整数闭区间的二元列表，按起点排序合并重叠或端点相同的区间；start>end 抛 ValueError；不修改输入。空输入返回空列表。", "merge_intervals", ("intervals",),
    (C("empty", ([],), []), C("overlap", ([[3, 5], [1, 3]],), [[1, 5]]), C("invalid", ([[3, 1]],), raises="ValueError")),
    (C("nested", ([[1, 8], [2, 3], [10, 11]],), [[1, 8], [10, 11]]), C("separate", ([[3, 4], [1, 2]],), [[1, 2], [3, 4]])),
    "def merge_intervals(intervals):\n    if any(a > b for a, b in intervals):\n        raise ValueError('invalid interval')\n    result = []\n    for start, end in sorted(intervals):\n        if result and start <= result[-1][1]:\n            result[-1][1] = max(result[-1][1], end)\n        else:\n            result.append([start, end])\n    return result\n"))

register_task_spec(TaskSpec("balanced-v1", "括号匹配", "实现 is_balanced(text)：判断 () [] {} 三类括号是否正确嵌套匹配，忽略其他字符，返回 bool；空串为 True。", "is_balanced", ("text",),
    (C("empty", ("",), True), C("nested", ("a{[()]}b",), True), C("cross", ("([)]",), False)),
    (C("open", ("((",), False), C("close", ("x]",), False), C("cross_nested", ("{(})",), False)),
    "def is_balanced(text):\n    stack = []\n    pairs = {')': '(', ']': '[', '}': '{'}\n    for char in text:\n        if char in '([{':\n            stack.append(char)\n        elif char in pairs:\n            if not stack or stack.pop() != pairs[char]:\n                return False\n    return not stack\n"))

register_task_spec(TaskSpec("moving-average-v1", "滑动均值", "实现 moving_average(values, window)：有限数值列表，返回每个完整窗口的均值列表；window 必须为正整数且 bool 不算整数，否则抛 ValueError；长度不足返回空列表，不修改输入。", "moving_average", ("values", "window"),
    (C("empty", ([], 2), []), C("normal", ([1, 2, 3], 2), [1.5, 2.5]), C("invalid", ([1], 0), raises="ValueError")),
    (C("too_short", ([1, 2], 3), []), C("bool_window", ([1], True), raises="ValueError"), C("unit", ([2, -2], 1), [2.0, -2.0])),
    "def moving_average(values, window):\n    if type(window) is not int or window <= 0:\n        raise ValueError('invalid window')\n    return [sum(values[i:i + window]) / window for i in range(len(values) - window + 1)]\n"))

register_task_spec(TaskSpec("slug-v1", "ASCII 标识生成", "实现 slugify(text)：转小写，仅保留 ASCII a-z 和 0-9，连续其他字符替换为一个连字符，去除首尾连字符；空输入返回空串。", "slugify", ("text",),
    (C("simple", ("Hello World!",), "hello-world"), C("empty", ("",), ""), C("runs", ("a___B",), "a-b")),
    (C("unicode", ("你好 A--2",), "a-2"), C("trim", ("--X--",), "x")),
    "import re\n\ndef slugify(text):\n    return re.sub(r'[^a-z0-9]+', '-', text.lower()).strip('-')\n"))

register_task_spec(TaskSpec("frequency-v1", "英文词频", "实现 count_words(text)：先按原文 ASCII [A-Za-z]+ 提取英文单词，再统一小写并返回词频字典，忽略数字、非 ASCII 字母和标点；空输入返回空字典。", "count_words", ("text",),
    (C("empty", ("",), {}), C("mixed", ("Hi, hi! BYE 42",), {"hi": 2, "bye": 1})),
    (C("separators", ("a-b a2B",), {"a": 2, "b": 2}), C("unicode", ("中文 TEST test",), {"test": 2}),
     C("non_ascii_case", ("İK TEST",), {"test": 1})),
    "import re\nfrom collections import Counter\n\ndef count_words(text):\n    return dict(Counter(word.lower() for word in re.findall(r'[A-Za-z]+', text)))\n"))
