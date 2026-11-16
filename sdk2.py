"""Offline SDK2 port: slots, explicit captures, closures; no name compiler."""
from dataclasses import dataclass
from values import array, clone, data, fail, fields, integer, plus, truth


@dataclass(frozen=True)
class Closure:
    arity: int
    body: dict
    captures: tuple


def validate(program):
    fields(program, ("sdk", "arity", "body"))
    if type(program["sdk"]) is not int or program["sdk"] != 2:
        fail("invalid_plan", "SDK2 program required")
    integer(program["arity"], "arity")
    validate_expr(program["body"])


def validate_expr(node):
    if type(node) is not dict:
        fail("invalid_plan", "expression must be an object")
    op = node.get("op")
    if op == "literal":
        fields(node, ("op", "value"))
        data(node["value"])
    elif op in ("local", "capture"):
        fields(node, ("op", "slot"))
        integer(node["slot"], "slot")
    elif op == "plus":
        fields(node, ("op", "left", "right"))
        validate_expr(node["left"])
        validate_expr(node["right"])
    elif op == "choose":
        fields(node, ("op", "test", "then", "else"))
        for key in ("test", "then", "else"):
            validate_expr(node[key])
    elif op == "closure":
        fields(node, ("op", "arity", "captures", "body"))
        integer(node["arity"], "arity")
        expressions(node["captures"])
        validate_expr(node["body"])
    elif op == "call":
        fields(node, ("op", "fn", "args"))
        validate_expr(node["fn"])
        expressions(node["args"])
    elif op == "array_map":
        fields(node, ("op", "array", "fn"))
        validate_expr(node["array"])
        validate_expr(node["fn"])
    elif op == "array_fold":
        fields(node, ("op", "array", "initial", "fn"))
        for key in ("array", "initial", "fn"):
            validate_expr(node[key])
    elif op == "pack":
        fields(node, ("op", "items"))
        expressions(node["items"])
    elif op == "item":
        fields(node, ("op", "array", "index"))
        integer(node["index"], "index")
        validate_expr(node["array"])
    elif op == "error":
        fields(node, ("op", "code", "message"))
        if node["code"] not in ("unbound", "type") or type(node["message"]) is not str:
            fail("invalid_plan", "invalid deferred error")
    else:
        fail("invalid_plan", "unknown SDK2 operation")


def expressions(items):
    if type(items) is not list:
        fail("invalid_plan", "expected expression list")
    for item in items:
        validate_expr(item)


def invoke(fn, args):
    if not isinstance(fn, Closure) or len(args) != fn.arity:
        fail("type", "closure arity mismatch")
    return evaluate(fn.body, tuple(args), fn.captures)


def execute(program, arguments):
    validate(program)
    if type(arguments) is not list or len(arguments) != program["arity"]:
        fail("invalid_params", "expected positional argument array of matching arity")
    args = tuple(clone(arg) for arg in arguments)
    result = evaluate(program["body"], args, ())
    data(result)
    return clone(result)


def evaluate(node, local, captures):
    op = node["op"]
    if op == "literal":
        return clone(node["value"])
    if op in ("local", "capture"):
        slots = local if op == "local" else captures
        slot = node["slot"]
        if slot >= len(slots):
            fail("unbound", f"{op}[{slot}]")
        return slots[slot]
    if op == "plus":
        left = evaluate(node["left"], local, captures)
        right = evaluate(node["right"], local, captures)
        return plus(left, right)
    if op == "choose":
        branch = "then" if truth(evaluate(node["test"], local, captures)) else "else"
        return evaluate(node[branch], local, captures)
    if op == "closure":
        bound = tuple(evaluate(expr, local, captures) for expr in node["captures"])
        return Closure(node["arity"], node["body"], bound)
    if op == "call":
        fn = evaluate(node["fn"], local, captures)
        args = [evaluate(expr, local, captures) for expr in node["args"]]
        return invoke(fn, args)
    if op == "array_map":
        source = array(evaluate(node["array"], local, captures))
        if source is None:
            return None
        result = []
        for item in source:
            fn = evaluate(node["fn"], local, captures)
            result.append(invoke(fn, [item]))
        return result
    if op == "array_fold":
        source = array(evaluate(node["array"], local, captures))
        if source is None:
            return None
        acc = evaluate(node["initial"], local, captures)
        for item in source:
            fn = evaluate(node["fn"], local, captures)
            acc = invoke(fn, [acc, item])
        return acc
    if op == "pack":
        return [evaluate(expr, local, captures) for expr in node["items"]]
    if op == "item":
        items = array(evaluate(node["array"], local, captures))
        if items is None or node["index"] >= len(items):
            fail("type", "environment item out of range")
        return items[node["index"]]
    fail(node["code"], node["message"])
