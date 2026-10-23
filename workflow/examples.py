"""Public, synthetic fixtures. Demo responses are explicitly scripted."""

ORDER_TASK = """实现 Python 函数 summarize_orders(orders)。输入是订单列表，每项有整数 amount_cents 和布尔 paid。
返回字典 paid_count 和 total_cents，只统计已付款订单。空列表返回两个 0，不修改输入。
所有订单都必须校验：缺少字段、金额不是非负整数（bool 不算整数）、paid 不是布尔值时抛出 ValueError。
只使用标准库，保留 summarize_orders(orders) 接口。"""

BUGGY_ORDERS = '''def summarize_orders(orders):
    paid_count = 0
    total_cents = 0
    for order in orders:
        if order["paid"]:
            paid_count += 1
            total_cents += amount_cents
    return {"paid_count": paid_count, "total_cents": total_cents}
'''

CORRECT_ORDERS = '''def summarize_orders(orders):
    paid_count = 0
    total_cents = 0
    for order in orders:
        if not isinstance(order, dict):
            raise ValueError("订单必须是字典")
        if "amount_cents" not in order or "paid" not in order:
            raise ValueError("订单缺少字段")
        amount = order["amount_cents"]
        paid = order["paid"]
        if type(amount) is not int or amount < 0 or type(paid) is not bool:
            raise ValueError("订单字段非法")
        if paid:
            paid_count += 1
            total_cents += amount
    return {"paid_count": paid_count, "total_cents": total_cents}
'''

# This harness is mounted into Docker and NEVER executed by the host interpreter.
ORDER_HARNESS = '''import copy
import json
import runpy

checks = []
try:
    namespace = runpy.run_path("/workspace/solution.py")
    function = namespace["summarize_orders"]
    cases = [
        ("empty", [], {"paid_count": 0, "total_cents": 0}),
        ("mixed", [{"amount_cents": 120, "paid": True}, {"amount_cents": 90, "paid": False}], {"paid_count": 1, "total_cents": 120}),
        ("zero", [{"amount_cents": 0, "paid": True}], {"paid_count": 1, "total_cents": 0}),
        ("missing", [{"paid": True}], "ValueError"),
        ("negative", [{"amount_cents": -1, "paid": False}], "ValueError"),
        ("bool_amount", [{"amount_cents": True, "paid": True}], "ValueError"),
        ("bool_paid", [{"amount_cents": 20, "paid": 1}], "ValueError"),
        ("bad_order", [None], "ValueError"),
    ]
    for name, source, expected in cases:
        before = copy.deepcopy(source)
        try:
            actual = function(source)
            valid = expected != "ValueError" and actual == expected and source == before
            detail = repr(actual)[:300]
        except Exception as exc:
            valid = expected == "ValueError" and isinstance(exc, ValueError) and source == before
            detail = type(exc).__name__
        checks.append({"id": name, "name": name, "status": "passed" if valid else "failed", "detail": detail, "expected": repr(expected)})
except Exception as exc:
    checks.append({"id": "load", "name": "载入函数", "status": "failed", "detail": type(exc).__name__})
print("CODELENS_RESULT=" + json.dumps(checks, ensure_ascii=False))
'''
