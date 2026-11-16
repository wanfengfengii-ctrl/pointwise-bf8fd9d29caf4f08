"""Business differential and ordinary failure checks; no optimized runtime."""
import argparse
from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import random
import sqlite3
import subprocess
import sys
import tempfile
import unittest
import sdk1
from catalog import Catalog
from jobs import Jobs
from samples import A, C, F, I, M, P, V
from storage import Database
from values import WarehouseError

ROOT = Path(__file__).resolve().parent
FROZEN = {
    "sdk1.py": "41fdab34c6a8e8334f82fc2aed6c8d38ffc03867383042ab88882ce99399ed8b",
    "tests/__init__.py": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
    "tests/test_runtime.py": "f862e429915ff992544949058ff8f898198ea0a830fc21303653c1213a3b5abe",
    "tests/test_storage.py": "fc0b5eba22dad4b69c15ac2455d62ed13b23171686cfcfa073d54079fc3c1f37",
}


def frozen():
    for relative, expected in FROZEN.items():
        assert hashlib.sha256((ROOT / relative).read_bytes()).hexdigest() == expected, relative


def outcome(action):
    try:
        return ("done", action())
    except WarehouseError as error:
        return ("failed", error.code, error.message)


def cases():
    source = M(V("rows"), "x", I(V("x"), C(5), V("late_source")))
    yield P(["rows"], F(source, V("initial_error"), "a", "x", V("body_error"))), {"rows": [1, 0]}
    yield P(["rows"], F(source, V("initial_error"), "a", "x", V("body_error"))), {"rows": []}
    yield P(["rows"], F(source, V("initial_error"), "a", "x", V("body_error"))), {"rows": None}
    yield P([], A(C([]), V("right_before_type"))), {}
    yield P([], A(C(None), C([]))), {}
    yield P([], I(C(1), C([7]), V("unselected"))), {}
    # The same stored array is consumed repeatedly, in two lexical scopes.
    repeated = M(V("rows"), "outer",
                 F(V("rows"), C(0), "a", "x", A(V("a"), V("x"))))
    yield P(["rows"], repeated), {"rows": [2, 3, 2]}
    intermediate = F(C([1]), M(V("rows"), "x", A(V("x"), C(1))), "a", "i",
                     M(V("a"), "x", V("a")))
    yield P(["rows"], intermediate), {"rows": [1, 2, 3]}
    # Accumulators are values, not necessarily scalars; repeated array results.
    yield P(["rows"], F(V("rows"), V("rows"), "a", "x", V("a"))), {"rows": [[1], [], None]}
    yield P(["rows"], F(V("rows"), C([]), "a", "x", V("x"))), {"rows": [[1], [2, None]]}
    yield P(["rows"], M(V("rows"), "rows", M(V("rows"), "rows", V("rows")))), {"rows": [[1, 2], []]}
    yield P([], M(C([]), "x", V("unused"))), {}
    yield P([], F(C(None), V("unused_initial"), "a", "x", V("unused"))), {}
    yield P([], F(C([]), C([1, None]), "a", "x", V("unused"))), {}
    yield P(["n"], A(V("n"), C(1))), {"n": 1 << 200}
    rng = random.Random(9107)
    def expr(depth):
        if depth == 0:
            return rng.choice([C(None), C(0), C(-7), C([1, None]), V("rows"), V("base"),
                               V("x"), V("a"), V("missing")])
        op = rng.randrange(6)
        if op == 0:
            return A(expr(depth - 1), expr(depth - 1))
        if op == 1:
            return I(expr(depth - 1), expr(depth - 1), expr(depth - 1))
        if op == 2:
            return M(expr(depth - 1), rng.choice(["rows", "x"]), expr(depth - 1))
        if op == 3:
            return F(expr(depth - 1), expr(depth - 1), "a", "x", expr(depth - 1))
        return expr(0)
    for _ in range(80):
        yield P(["rows", "base"], expr(3)), {
            "rows": rng.choice([None, [], [1, 0, None], [[1], [], None]]),
            "base": rng.choice([None, 0, 7])}


def differential():
    with Database() as db:
        catalog, jobs = Catalog(db), Jobs(db)
        count = 0
        for plan, parameters in cases():
            expected = outcome(lambda: sdk1.execute(plan, parameters))
            revision = catalog.save("p", plan)
            job = jobs.enqueue("p", parameters)
            catalog.save("p", P(plan["parameters"], C(-999)))
            state = jobs.run(job)
            actual = outcome(lambda: jobs.result(job))
            assert actual == expected, (plan, parameters, expected, actual)
            assert state == expected[0] and jobs.status(job)["revision"] == revision
            assert jobs.run(job) == state
            count += 1
        assert db.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    print(json.dumps({"differential_cases": count, "checked": True}))


class CommitFaultDatabase(Database):
    armed = False

    def transaction(self, before_commit=None):
        def hook(connection):
            if before_commit is not None:
                before_commit(connection)
            if self.armed:
                self.armed = False
                raise RuntimeError("injected precommit failure")
        return super().transaction(before_commit=hook)


@contextmanager
def workspace():
    previous = Path.cwd()
    saved_env = {key: os.environ.get(key) for key in ("TMPDIR", "TEMP", "TMP")}
    saved_tempdir = tempfile.tempdir
    with tempfile.TemporaryDirectory(prefix="array-memory-fault-") as directory:
        path = Path(directory).resolve()
        try:
            os.chdir(path)
            for key in saved_env:
                os.environ[key] = str(path)
            tempfile.tempdir = str(path)
            yield path
        finally:
            os.chdir(previous)
            for key, value in saved_env.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            tempfile.tempdir = saved_tempdir


def disk_case(mode):
    with workspace() as directory:
        path = directory / "warehouse.sqlite"
        with CommitFaultDatabase(path) as db:
            catalog, jobs = Catalog(db), Jobs(db)
            source = M(V("rows"), "x", I(V("x"), C([7, None]), V("late_source")))
            catalog.save("p", P(["rows"], source))
            job = jobs.enqueue("p", {"rows": [1] * 2000 + ([0] if mode == "business" else [1])})
        with CommitFaultDatabase(path) as db:
            jobs = Jobs(db)
            if mode == "commit":
                db.armed = True
            if mode == "write":
                def deny(action, table, column, database, trigger):
                    if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE) and table == "jobs_v1":
                        return sqlite3.SQLITE_DENY
                    return sqlite3.SQLITE_OK
                db.connection.set_authorizer(deny)
            if mode in ("commit", "write"):
                exception = RuntimeError if mode == "commit" else sqlite3.DatabaseError
                try:
                    jobs.run(job)
                except exception:
                    pass
                else:
                    raise AssertionError("injected nonbusiness failure did not escape")
                db.connection.set_authorizer(None)
                assert jobs.status(job)["state"] == "queued"
                assert outcome(lambda: jobs.result(job)) == ("failed", "pending", "job has not completed")
                raw = db.connection.execute("SELECT output,error FROM jobs_v1 WHERE id=?", (job,)).fetchone()
                assert tuple(raw) == (None, None)
            elif mode == "business":
                assert jobs.run(job) == "failed"
                assert outcome(lambda: jobs.result(job)) == ("failed", "unbound", "late_source")
            else:
                assert jobs.run(job) == "done"
                assert jobs.result(job) == [[7, None]] * 2001
        # Ordinary files/directories in the dedicated scratch root must be gone.
        assert set(directory.iterdir()) == {path}, sorted(p.name for p in directory.iterdir())
        with Database(path) as db:
            jobs = Jobs(db)
            if mode in ("commit", "write"):
                assert jobs.status(job)["state"] == "queued"
                assert jobs.run(job) == "done"
            if mode == "business":
                assert outcome(lambda: jobs.result(job)) == ("failed", "unbound", "late_source")
            else:
                assert jobs.result(job) == [[7, None]] * 2001
            assert db.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert set(directory.iterdir()) == {path}


def robustness():
    for mode in ("success", "business", "write", "commit"):
        disk_case(mode)
    print(json.dumps({"disk_failure_modes": ["success", "business", "write", "commit"],
                      "reopen_retry_checked": True, "scratch_root_checked": True}))


def verify(mode):
    frozen()
    suite = unittest.defaultTestLoader.discover(str(ROOT / "tests"))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    assert result.wasSuccessful(), "original regressions failed"
    differential()
    robustness()
    subprocess.run([sys.executable, "-B", str(ROOT / "memory_bench.py"), mode],
                   cwd=ROOT, check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("baseline", "target", "semantics"))
    args = parser.parse_args()
    if args.mode == "semantics":
        frozen()
        differential()
        robustness()
    else:
        verify(args.mode)


if __name__ == "__main__":
    main()
