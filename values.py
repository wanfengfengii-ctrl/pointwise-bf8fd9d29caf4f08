"""Shared JSON data and stable public errors (Python 3.11)."""
import json


class WarehouseError(Exception):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def fail(code, message):
    raise WarehouseError(code, message)


def data(value):
    """Accept only null, exact integers, and recursively nested arrays."""
    if value is None or type(value) is int:
        return
    if type(value) is list:
        for item in value:
            data(item)
        return
    fail("invalid_value", "expected null, integer, or array")


def clone(value):
    data(value)
    if type(value) is list:
        return [clone(item) for item in value]
    return value


def dumps(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def name(value):
    if type(value) is not str or not value:
        fail("invalid_plan", "names must be nonempty strings")


def fields(node, expected):
    if type(node) is not dict or set(node) != set(expected):
        fail("invalid_plan", f"expected fields {sorted(expected)}")


def integer(value, label):
    if type(value) is not int or value < 0:
        fail("invalid_plan", f"{label} must be a nonnegative integer")


def plus(left, right):
    if left is not None and type(left) is not int:
        fail("type", "addition expects nullable integers")
    if right is not None and type(right) is not int:
        fail("type", "addition expects nullable integers")
    return None if left is None or right is None else left + right


def truth(value):
    if value is not None and type(value) is not int:
        fail("type", "condition expects a nullable integer")
    return value is not None and value != 0


def array(value):
    if value is not None and type(value) is not list:
        fail("type", "array operation expects an array or null")
    return value
