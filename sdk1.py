"""SDK1: eager array evaluation with lexical, name-based environments."""
from values import array, clone, data, fail, fields, name, plus, truth


def validate(plan):
    fields(plan, ("sdk", "parameters", "body"))
    if type(plan["sdk"]) is not int or plan["sdk"] != 1:
        fail("invalid_plan", "SDK1 plan required")
    params = plan["parameters"]
    if type(params) is not list:
        fail("invalid_plan", "parameters must be an ordered name list")
    for param in params:
        name(param)
    if len(set(params)) != len(params):
        fail("invalid_plan", "duplicate parameter")
    validate_expr(plan["body"])


def validate_expr(node):
    if type(node) is not dict:
        fail("invalid_plan", "expression must be an object")
    op = node.get("op")
    if op == "const":
        fields(node, ("op", "value"))
        data(node["value"])
    elif op == "var":
        fields(node, ("op", "name"))
        name(node["name"])
    elif op == "add":
        fields(node, ("op", "left", "right"))
        validate_expr(node["left"])
        validate_expr(node["right"])
    elif op == "if":
        fields(node, ("op", "test", "then", "else"))
        for key in ("test", "then", "else"):
            validate_expr(node[key])
    elif op == "map":
        fields(node, ("op", "array", "param", "body"))
        name(node["param"])
        validate_expr(node["array"])
        validate_expr(node["body"])
    elif op == "fold":
        fields(node, ("op", "array", "initial", "acc", "item", "body"))
        name(node["acc"])
        name(node["item"])
        if node["acc"] == node["item"]:
            fail("invalid_plan", "fold parameters must differ")
        for key in ("array", "initial", "body"):
            validate_expr(node[key])
    else:
        fail("invalid_plan", "unknown SDK1 operation")


def bind(plan, parameters):
    if type(parameters) is not dict or set(parameters) != set(plan["parameters"]):
        fail("invalid_params", "parameters must match the declared names exactly")
    return {key: clone(value) for key, value in parameters.items()}


def execute(plan, parameters):
    validate(plan)
    return evaluate(plan["body"], bind(plan, parameters))


def evaluate(node, env):
    op = node["op"]
    if op == "const":
        return clone(node["value"])
    if op == "var":
        key = node["name"]
        if key not in env:
            fail("unbound", key)
        return env[key]
    if op == "add":
        left = evaluate(node["left"], env)
        right = evaluate(node["right"], env)
        return plus(left, right)
    if op == "if":
        branch = "then" if truth(evaluate(node["test"], env)) else "else"
        return evaluate(node[branch], env)
    if op == "map":
        source = array(evaluate(node["array"], env))
        if source is None:
            return None
        result = []
        for item in source:
            local = dict(env)
            local[node["param"]] = item
            result.append(evaluate(node["body"], local))
        return result
    source = array(evaluate(node["array"], env))
    if source is None:
        return None
    acc = evaluate(node["initial"], env)
    for item in source:
        local = dict(env)
        local[node["acc"]] = acc
        local[node["item"]] = item
        acc = evaluate(node["body"], local)
    return acc
