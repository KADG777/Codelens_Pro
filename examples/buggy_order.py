"""订单计算示例：有意保留问题，供审查演示使用。"""


def add_order(order, orders=[]):
    orders.append(order)
    return orders


def average_price(prices):
    return sum(prices) / len(prices)


def load_discount(text):
    try:
        return float(text)
    except:
        return 0


def calculate_fee(amount):
    return amount / 0
