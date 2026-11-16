import copy
import unittest
import sdk1
import sdk2
from samples import A, C, F, I, M, P, V, frozen_rows, native_program, nested, offset
from values import WarehouseError


class ErrorAssertions(unittest.TestCase):
    def error(self, code, action, message=None):
        with self.assertRaises(WarehouseError) as caught:
            action()
        self.assertEqual(caught.exception.code, code)
        if message is not None:
            self.assertEqual(caught.exception.message, message)


class SDK1Tests(ErrorAssertions):
    def test_nested_fold_shadow_and_capture(self):
        args = {"rows": [[10, 20], [], [None, 10]], "base": 7}
        original = copy.deepcopy(args)
        self.assertEqual(sdk1.execute(nested(), args), [[37, 57], [], [None, 37]])
        self.assertEqual(args, original)

    def test_same_name_does_not_leak(self):
        body = A(
            F(C([10, 20]), C(0), "acc", "x",
              A(V("acc"), F(C([2, 3]), V("x"), "acc", "x", A(V("acc"), V("x"))))),
            V("x"),
        )
        self.assertEqual(sdk1.execute(P(["x"], body), {"x": 100}), 140)
        shadow = M(C([10]), "x", M(C([2, 2]), "x", A(V("x"), V("base"))))
        self.assertEqual(sdk1.execute(P(["x", "base"], shadow), {"x": 99, "base": 7}),
                         [[9, 9]])

    def test_capture_across_two_scopes(self):
        body = M(C([10, 20]), "x",
                 M(C([1]), "y", M(C([2, 2]), "y", A(V("x"), V("y")))))
        self.assertEqual(sdk1.execute(P([], body), {}), [[[12, 12]], [[22, 22]]])

    def test_null_empty_order_duplicates(self):
        self.assertEqual(sdk1.execute(offset(), {"rows": [2, 0, 2, None], "base": 7}),
                         [9, 7, 9, None])
        self.assertIsNone(sdk1.execute(offset(), {"rows": None, "base": 7}))
        self.assertEqual(sdk1.execute(offset(), {"rows": [], "base": 7}), [])
        self.assertEqual(sdk1.execute(P([], F(C([]), C([1]), "a", "x", V("bad"))), {}),
                         [1])
        self.assertIsNone(sdk1.execute(
            P([], F(C(None), V("bad_seed"), "a", "x", V("bad_body"))), {}))

    def test_conditions_are_lazy_and_integer_based(self):
        for gate in (1, -3):
            self.assertEqual(sdk1.execute(P(["gate"], I(V("gate"), C(7), V("bad"))),
                                          {"gate": gate}), 7)
        for gate in (0, None):
            self.assertEqual(sdk1.execute(P(["gate"], I(V("gate"), V("bad"), C(8))),
                                          {"gate": gate}), 8)
        self.assertEqual(sdk1.execute(P([], M(C([]), "x", V("bad"))), {}), [])

    def test_error_evaluation_order(self):
        self.error("unbound", lambda: sdk1.execute(P([], A(C([]), V("right"))), {}),
                   "right")
        mapped = M(C([1, 0]), "x", I(V("x"), C(5), V("map_error")))
        expr = F(mapped, V("seed_error"), "a", "x", V("body_error"))
        self.error("unbound", lambda: sdk1.execute(P([], expr), {}), "map_error")
        self.error("unbound", lambda: sdk1.execute(
            P([], F(C([]), V("seed_error"), "a", "x", V("body_error"))), {}),
            "seed_error")

    def test_runtime_errors(self):
        for expr in (A(C(None), C([])), I(C([]), C(1), C(2)), M(C(9), "x", V("x"))):
            self.error("type", lambda expr=expr: sdk1.execute(P([], expr), {}))
        self.error("unbound", lambda: sdk1.execute(P([], V("missing")), {}), "missing")

    def test_parameter_and_ast_validation(self):
        self.error("invalid_params", lambda: sdk1.execute(offset(), {"rows": []}))
        self.error("invalid_params", lambda: sdk1.execute(P([], C(1)), {"extra": 1}))
        self.error("invalid_value", lambda: sdk1.execute(P(["a"], V("a")), {"a": True}))
        bad = [
            P(["a", "a"], C(1)), P([], F(C([]), C(0), "a", "a", C(0))),
            P([], {"op": "add", "left": C(1)}),
            P([], {"op": "var", "name": ""}),
            {"sdk": 2, "parameters": [], "body": C(1)},
            P([], I(C(1), C(7), {"op": "unknown"})),
        ]
        for plan in bad:
            self.error("invalid_plan", lambda plan=plan: sdk1.validate(plan))

    def test_results_do_not_alias_inputs_or_constants(self):
        plan = P(["rows"], V("rows"))
        args = {"rows": [[1]]}
        result = sdk1.execute(plan, args)
        result[0][0] = 8
        self.assertEqual(args, {"rows": [[1]]})
        plan = P([], C([[2]]))
        result = sdk1.execute(plan, {})
        result[0][0] = 9
        self.assertEqual(sdk1.execute(plan, {}), [[2]])

    def test_frozen_historical_literals_match_old_runtime(self):
        plans, jobs = frozen_rows()
        index = {(name, revision): plan for name, revision, plan in plans}
        expected = {1: [9, 9, None], 2: [[37, 57], [], [None, 37]], 3: [], 5: 5, 6: None}
        for job_id, name, revision, parameters, state, output, error in jobs:
            plan = index[name, revision]
            if error is not None:
                self.error(error["code"],
                           lambda plan=plan, parameters=parameters: sdk1.execute(plan, parameters),
                           error["message"])
            else:
                self.assertEqual(sdk1.execute(plan, parameters), expected[job_id])
                if state == "done":
                    self.assertEqual(output, expected[job_id])


def lit(value):
    return {"op": "literal", "value": value}


def loc(slot):
    return {"op": "local", "slot": slot}


def cap(slot):
    return {"op": "capture", "slot": slot}


def plus(left, right):
    return {"op": "plus", "left": left, "right": right}


def closure(arity, captures, body):
    return {"op": "closure", "arity": arity, "captures": captures, "body": body}


def program(arity, body):
    return {"sdk": 2, "arity": arity, "body": body}


class SDK2Tests(ErrorAssertions):
    def test_flat_and_environment_record_ports(self):
        for record in (False, True):
            self.assertEqual(sdk2.execute(native_program(record), [[2, 0, 2, None], 7]),
                             [9, 7, 9, None])

    def test_explicit_multilevel_capture_and_fold_two_locals(self):
        reducer = closure(2, [cap(0), cap(1), loc(0)],
                          plus(loc(0), plus(loc(1), plus(cap(0), plus(cap(1), cap(2))))))
        fold = {"op": "array_fold", "array": lit([3, 4]), "initial": lit(0), "fn": reducer}
        middle = {"op": "array_map", "array": lit([1, 2]),
                  "fn": closure(1, [loc(0), cap(0)], fold)}
        outer = {"op": "array_map", "array": loc(0),
                 "fn": closure(1, [loc(1)], middle)}
        self.assertEqual(sdk2.execute(program(2, outer), [[10, 20], 7]),
                         [[43, 45], [63, 65]])

    def test_no_implicit_parent_locals(self):
        body = {"op": "call", "fn": closure(0, [], loc(0)), "args": []}
        self.error("unbound", lambda: sdk2.execute(program(1, body), [99]), "local[0]")
        captured = {"op": "call", "fn": closure(0, [loc(0)], cap(0)), "args": []}
        self.assertEqual(sdk2.execute(program(1, captured), [99]), 99)

    def test_call_and_closure_arity(self):
        body = {"op": "call", "fn": closure(1, [lit(7)], plus(loc(0), cap(0))),
                "args": [lit(2)]}
        self.assertEqual(sdk2.execute(program(0, body), []), 9)
        body["args"] = []
        self.error("type", lambda: sdk2.execute(program(0, body), []))
        body["fn"] = lit(7)
        self.error("type", lambda: sdk2.execute(program(0, body), []))

    def test_lazy_branches_empty_and_null_arrays(self):
        bad = {"op": "error", "code": "unbound", "message": "unused"}
        choice = {"op": "choose", "test": lit(None), "then": bad, "else": lit(7)}
        self.assertEqual(sdk2.execute(program(0, choice), []), 7)
        body = {"op": "array_map", "array": lit([]),
                "fn": closure(1, [bad], loc(0))}
        self.assertEqual(sdk2.execute(program(0, body), []), [])
        body["array"] = lit(None)
        self.assertIsNone(sdk2.execute(program(0, body), []))
        body = {"op": "array_fold", "array": lit(None), "initial": bad, "fn": bad}
        self.assertIsNone(sdk2.execute(program(0, body), []))
        body.update(array=lit([]), initial=lit(4))
        self.assertEqual(sdk2.execute(program(0, body), []), 4)

    def test_captures_evaluate_at_creation_in_order(self):
        bad1 = {"op": "error", "code": "unbound", "message": "first"}
        bad2 = {"op": "error", "code": "unbound", "message": "second"}
        body = {"op": "call", "fn": closure(0, [bad1, bad2], lit(7)), "args": []}
        self.error("unbound", lambda: sdk2.execute(program(0, body), []), "first")

    def test_validation_and_positional_arguments(self):
        self.error("invalid_params", lambda: sdk2.execute(native_program(), [1]))
        self.error("invalid_params", lambda: sdk2.execute(native_program(), {"rows": []}))
        self.error("invalid_plan", lambda: sdk2.execute(program(0, loc(-1)), []))
        self.error("invalid_plan", lambda: sdk2.validate(program(0, {"op": "var", "name": "x"})))
        self.error("type", lambda: sdk2.execute(
            program(0, {"op": "item", "array": lit([]), "index": 0}), []))
        self.error("unbound", lambda: sdk2.execute(program(0, cap(0)), []), "capture[0]")
        self.error("invalid_value", lambda: sdk2.execute(program(0, closure(0, [], lit(1))), []))


if __name__ == "__main__":
    unittest.main()
