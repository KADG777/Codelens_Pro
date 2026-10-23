"""处理边界情况的订单计算示例。"""


def add_order(order, orders=None):
    if orders is None:
        orders = []
    orders.append(order)
    return orders


def average_price(prices):
    if not prices:
        raise ValueError("prices must not be empty")
    return sum(prices) / len(prices)


def load_discount(text):
    try:
        return float(text)
    except (TypeError, ValueError) as exc:
        raise ValueError("discount must be a number") from exc
