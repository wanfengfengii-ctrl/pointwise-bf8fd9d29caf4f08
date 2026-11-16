"""Finite business samples and an SDK1 frozen-library constructor.

The constructor uses raw SQLite, the frozen schema and literal historical rows.
It never calls Catalog, Jobs, SDK1 or SDK2.
"""
import json
import sqlite3
from legacy_format import install


def C(value):
    return {"op": "const", "value": value}


def V(name):
    return {"op": "var", "name": name}


def A(left, right):
    return {"op": "add", "left": left, "right": right}


def I(test, yes, no):
    return {"op": "if", "test": test, "then": yes, "else": no}


def M(source, param, body):
    return {"op": "map", "array": source, "param": param, "body": body}


def F(source, initial, acc, item, body):
    return {"op": "fold", "array": source, "initial": initial,
            "acc": acc, "item": item, "body": body}


def P(parameters, body):
    return {"sdk": 1, "parameters": parameters, "body": body}


def offset():
    return P(["rows", "base"], M(V("rows"), "x", A(V("x"), V("base"))))


def nested():
    return P(["rows", "base"],
             M(V("rows"), "row",
               M(V("row"), "x",
                 F(C([1, 2]), C(0), "acc", "row",
                   A(V("acc"), A(V("x"), A(V("row"), V("base"))))))))


def frozen_rows():
    plans = [
        ("offset", 1, offset()),
        ("offset", 2, P(["rows", "base"], C(999))),
        ("nested", 1, nested()),
        ("lazy", 1, P(["gate"], I(V("gate"), C(5), V("missing")))),
    ]
    jobs = [
        (1, "offset", 1, {"rows": [2, 2, None], "base": 7}, "queued", None, None),
        (2, "nested", 1, {"rows": [[10, 20], [], [None, 10]], "base": 7},
         "queued", None, None),
        (3, "offset", 1, {"rows": [], "base": 7}, "done", [], None),
        (4, "lazy", 1, {"gate": 0}, "failed", None,
         {"code": "unbound", "message": "missing"}),
        (5, "lazy", 1, {"gate": 1}, "queued", None, None),
        (6, "offset", 1, {"rows": None, "base": 7}, "done", None, None),
    ]
    return plans, jobs


def create_frozen(path):
    """Create a complete historical database at an empty destination."""
    connection = sqlite3.connect(str(path), isolation_level=None)
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table'").fetchone():
            raise ValueError("frozen constructor requires an empty database")
        install(connection)
        plans, jobs = frozen_rows()
        heads = {}
        for name, revision, plan in plans:
            connection.execute("INSERT INTO plans_v1 VALUES (?,?,?)",
                               (name, revision, json.dumps(plan, sort_keys=True)))
            heads[name] = max(heads.get(name, 0), revision)
        for name, revision in heads.items():
            connection.execute("INSERT INTO heads_v1 VALUES (?,?)", (name, revision))
        for job_id, name, revision, parameters, state, output, error in jobs:
            connection.execute("INSERT INTO jobs_v1 VALUES (?,?,?,?,?,?,?)",
                               (job_id, name, revision, json.dumps(parameters), state,
                                json.dumps(output) if state == "done" else None,
                                json.dumps(error) if error is not None else None))
        connection.commit()
    except BaseException:
        connection.rollback()
        raise
    finally:
        connection.close()


def native_program(environment_record=False):
    """Handwritten SDK2 port example, unrelated to catalog storage."""
    captured = {"op": "local", "slot": 1}
    reference = {"op": "capture", "slot": 0}
    if environment_record:
        captured = {"op": "pack", "items": [captured]}
        reference = {"op": "item", "array": reference, "index": 0}
    return {
        "sdk": 2, "arity": 2,
        "body": {
            "op": "array_map", "array": {"op": "local", "slot": 0},
            "fn": {"op": "closure", "arity": 1, "captures": [captured],
                   "body": {"op": "plus", "left": {"op": "local", "slot": 0},
                            "right": reference}},
        },
    }
