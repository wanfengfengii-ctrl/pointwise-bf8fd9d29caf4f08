"""Fixed run-only memory workloads; no optimized execution code."""
from samples import A, C, F, I, M, P, V, nested


KINDS = ("nested", "reduce")
SIZES = (100000, 400000)


def row(index):
    return [index % 997, None if index % 7 == 0 else -index, (1 << 70) + index]


def plan(kind):
    if kind == "nested":
        return nested()
    if kind == "reduce":
        subtotal = F(V("row"), C(0), "a", "x",
                     A(V("a"), I(V("x"), V("x"), C(0))))
        return P(["rows", "base"],
                 F(M(V("rows"), "row", subtotal), C(0), "a", "x",
                   A(V("a"), V("x"))))
    raise ValueError(kind)


def parameters(size):
    return {"rows": [row(i) for i in range(size)], "base": 7}


def verify_value(kind, size, value):
    if kind == "nested":
        assert type(value) is list and len(value) == size
        for i, actual in enumerate(value):
            expected = [None if x is None else 2 * x + 17 for x in row(i)]
            assert actual == expected, (i, actual, expected)
    elif kind == "reduce":
        expected = sum(sum(x for x in row(i) if x is not None) for i in range(size))
        assert type(value) is int and value == expected, (value, expected)
    else:
        raise ValueError(kind)
