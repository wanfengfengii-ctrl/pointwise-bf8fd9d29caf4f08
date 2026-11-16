"""Independent differential, representation and exception verification.

This module deliberately does not reuse the fixtures in ``memory_checks.py``.
It compares the shipped ``Jobs.run`` against the frozen ``sdk1`` reference on
its own case set, pins the stored representations (parameter snapshot and
output document) byte for byte, and exercises the new temporary resources on
every exit path (success, business failure, ordinary read/write failure,
pre-commit failure) while checking that nothing is left behind.

Run as::

    python3.11 -B run_checks.py            # differential + invariants + faults
    python3.11 -B run_checks.py --verbose  # same, with per-section lines
"""
import argparse
import copy
import json
import os
from pathlib import Path
import random
import sqlite3
import sys
import tempfile

import sdk1
from catalog import Catalog
from jobs import Jobs
from samples import A, C, F, I, M, P, V, create_frozen
from storage import Database
from values import WarehouseError, dumps

ROOT = Path(__file__).resolve().parent
LINE = []


def say(*parts):
    text = " ".join(str(part) for part in parts)
    LINE.append(text)
    print(text, flush=True)


def outcome(action):
    try:
        return ("done", action())
    except WarehouseError as error:
        return ("failed", error.code, error.message)


def compact(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def blob_bytes(connection, table, column, rowid):
    blob = connection.blobopen(table, column, rowid, readonly=True)
    try:
        pieces = []
        while True:
            chunk = blob.read(1 << 16)
            if not chunk:
                break
            pieces.append(chunk)
        return b"".join(pieces)
    finally:
        blob.close()


def temp_tables(connection):
    return sorted(row[0] for row in connection.execute(
        "SELECT name FROM sqlite_temp_master WHERE type='table'"))


def canonical_rows(size):
    return [[index % 997, None if index % 7 == 0 else -index, (1 << 70) + index]
            for index in range(size)]


# --------------------------------------------------------------------------
# 1. Differential against the frozen sdk1 reference.
# --------------------------------------------------------------------------

def _cases():
    """Plans derived here, independent of the fixtures in memory_checks."""
    # Scalar / null / empty and lazy ordering.
    yield P([], A(C(None), V("late"))), {}
    yield P([], A(V("left_late"), C([]))), {}
    yield P(["g"], I(V("g"), C([1, 2]), V("else_late"))), {"g": 0}
    yield P(["g"], I(V("g"), V("then_late"), C(3))), {"g": None}
    yield P([], M(C(None), "x", V("body_late"))), {}
    yield P([], M(C([]), "x", V("body_late"))), {}
    yield P([], F(C(None), V("seed_late"), "a", "x", V("body_late"))), {}
    yield P([], F(C([]), C([None, 1]), "a", "x", V("body_late"))), {}
    # Complete map source precedes the fold initial value.
    mapped = M(V("rows"), "x", I(V("x"), C(7), V("map_late")))
    yield P(["rows"], F(mapped, V("initial_late"), "a", "x", V("body_late"))), {"rows": [1, 0]}
    yield P(["rows"], F(mapped, V("initial_late"), "a", "x", V("body_late"))), {"rows": []}
    yield P(["rows"], F(mapped, V("initial_late"), "a", "x", V("body_late"))), {"rows": None}
    # Repeated references to one stored array, in two lexical layers.
    inner = F(V("rows"), C(0), "a", "x", A(V("a"), V("x")))
    yield P(["rows"], M(V("rows"), "rows", inner)), {"rows": [4, 5, None]}
    yield P(["rows"], F(V("rows"), V("rows"), "a", "x", V("a"))), {"rows": [[1], [], None]}
    yield P(["rows"], M(V("rows"), "x", A(V("x"), V("x")))), {"rows": [2, None, -3]}
    # Array-valued accumulators and shadowing.
    yield P(["rows"], F(V("rows"), C([]), "a", "x", V("x"))), {"rows": [[1], [2, None]]}
    yield P(["rows"], F(V("rows"), C([0]), "a", "x",
                       M(V("a"), "a", A(V("a"), V("x"))))), {"rows": [1, 2]}
    shadow = M(C([7]), "rows", M(V("rows"), "rows", V("rows")))
    yield P(["rows"], shadow), {"rows": [9]}
    # Exact integers without truncation.
    yield P(["n"], A(V("n"), C(1))), {"n": 1 << 200}
    yield P(["n"], F(C([1, 2]), V("n"), "a", "x", A(V("a"), V("x")))), {"n": -(1 << 96)}
    # A parameter name that collides with the large-snapshot marker text.
    yield P(["$stored"], V("$stored")), {"$stored": [5, None]}
    # Random trees over the same operators.
    rng = random.Random(20261116)

    def expr(depth):
        if depth == 0:
            return rng.choice([C(None), C(0), C(-11), C([3, None]), V("rows"),
                               V("base"), V("x"), V("a"), V("nope")])
        choice = rng.randrange(6)
        if choice == 0:
            return A(expr(depth - 1), expr(depth - 1))
        if choice == 1:
            return I(expr(depth - 1), expr(depth - 1), expr(depth - 1))
        if choice == 2:
            return M(expr(depth - 1), rng.choice(["rows", "x"]), expr(depth - 1))
        if choice == 3:
            return F(expr(depth - 1), expr(depth - 1), "a", "b", expr(depth - 1))
        return expr(0)

    for _ in range(120):
        yield P(["rows", "base"], expr(3)), {
            "rows": rng.choice([None, [], [1, 0, None], [[2], [], None]]),
            "base": rng.choice([None, 0, 7])}


def differential():
    with Database() as db:
        catalog, jobs = Catalog(db), Jobs(db)
        count = 0
        for plan, parameters in _cases():
            expected = outcome(lambda: sdk1.execute(plan, parameters))
            revision = catalog.save("diff", plan)
            job = jobs.enqueue("diff", parameters)
            catalog.save("diff", P(plan["parameters"], C(-777)))
            state = jobs.run(job)
            actual = outcome(lambda: jobs.result(job))
            assert (state,) + tuple(actual[1:]) == expected[:1] + expected[1:], \
                (plan, parameters, expected, actual, state)
            assert state == expected[0]
            assert jobs.status(job)["revision"] == revision
            before = tuple(db.connection.execute(
                "SELECT * FROM jobs_v1 WHERE id=?", (job,)).fetchone())
            assert jobs.run(job) == state
            assert tuple(db.connection.execute(
                "SELECT * FROM jobs_v1 WHERE id=?", (job,)).fetchone()) == before
            count += 1
        assert temp_tables(db.connection) == []
        assert db.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    say("differential_cases", count, "ok")


# --------------------------------------------------------------------------
# 2. Stored-representation invariants.
# --------------------------------------------------------------------------

def representation():
    sizes = [0, 1, 2047, 2048, 2049, 5000, 7000, 9000, 20000]
    large_job = None
    with tempfile.TemporaryDirectory(prefix="run-checks-repr-") as directory:
        path = Path(directory) / "repr.sqlite"
        with Database(path) as db:
            catalog, jobs = Catalog(db), Jobs(db)
            catalog.save("id", P(["rows"], V("rows")))
            catalog.save("map", P(["rows", "base"], M(V("rows"), "x", A(V("x"), V("base")))))
            for size in sizes:
                rows = canonical_rows(size)
                parameters = {"rows": rows}
                canonical = dumps(sdk1.bind({"parameters": ["rows"]}, parameters))
                job = jobs.enqueue("id", parameters)
                stored = db.connection.execute(
                    "SELECT parameters, typeof(parameters) FROM jobs_v1 WHERE id=?",
                    (job,)).fetchone()
                companion = db.connection.execute(
                    "SELECT count(*) FROM sqlite_master WHERE type='table' "
                    "AND name='job_parameters_v1'").fetchone()[0]
                if companion and db.connection.execute(
                        "SELECT 1 FROM job_parameters_v1 WHERE job_id=?", (job,)
                ).fetchone():
                    assert json.loads(stored[0]) == {"$stored": len(canonical.encode())}
                    raw = blob_bytes(db.connection, "job_parameters_v1", "data", job)
                else:
                    raw = stored[0].encode("utf-8")
                assert raw.decode("utf-8") == canonical, (size, raw[:40])
                assert jobs.run(job) == "done"
                value = jobs.result(job)
                assert value == rows, size
                out = db.connection.execute(
                    "SELECT output, typeof(output) FROM jobs_v1 WHERE id=?",
                    (job,)).fetchone()
                if out[1] == "blob":
                    stored_out = blob_bytes(db.connection, "jobs_v1", "output", job)
                else:
                    stored_out = out[0].encode("utf-8")
                assert stored_out.decode("utf-8") == compact(rows), size
                assert json.loads(stored_out) == rows
                assert temp_tables(db.connection) == []
                if size == 9000:
                    large_job = job
            # A small output keeps the frozen 'null' spelling.
            catalog.save("nil", P([], C(None)))
            nil_job = jobs.enqueue("nil", {})
            assert jobs.run(nil_job) == "done"
            assert db.connection.execute(
                "SELECT output, typeof(output) FROM jobs_v1 WHERE id=?",
                (nil_job,)).fetchone()[0] == "null"
        # Closed-and-reopened reads see the complete large value.
        with Database(path) as db:
            jobs = Jobs(db)
            rows = canonical_rows(9000)
            assert jobs.result(large_job) == rows
            assert jobs.status(large_job)["revision"] == 1
            assert db.connection.execute("PRAGMA foreign_key_check").fetchall() == []
    say("representation_sizes", sizes, "ok")


# --------------------------------------------------------------------------
# 3. Exception injection and resource ownership.
# --------------------------------------------------------------------------

class HookDatabase(Database):
    armed = False

    def transaction(self, before_commit=None):
        def hook(connection):
            if before_commit is not None:
                before_commit(connection)
            if self.armed:
                self.armed = False
                raise RuntimeError("injected pre-commit failure")
        return super().transaction(before_commit=hook)


def open_files():
    try:
        return len(os.listdir("/proc/self/fd"))
    except OSError:
        return -1


def deny(table_name, actions):
    def authorizer(action, table, column, database, trigger):
        if action in actions and table == table_name:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    return authorizer


def exception_paths():
    with tempfile.TemporaryDirectory(prefix="run-checks-fault-") as directory:
        root = Path(directory).resolve()
        previous = Path.cwd()
        saved = {key: os.environ.get(key) for key in ("TMPDIR", "TEMP", "TMP")}
        saved_tempdir = tempfile.tempdir
        try:
            os.chdir(root)
            for key in saved:
                os.environ[key] = str(root)
            tempfile.tempdir = str(root)
            path = root / "warehouse.sqlite"
            rows = canonical_rows(60000)
            # Enqueue failing after the marker row is inserted leaves nothing.
            with HookDatabase(path) as db:
                catalog, jobs = Catalog(db), Jobs(db)
                catalog.save("id", P(["rows"], V("rows")))
                db.connection.set_authorizer(deny(
                    "job_parameters_v1", (sqlite3.SQLITE_INSERT,)))
                try:
                    jobs.enqueue("id", {"rows": rows})
                except sqlite3.DatabaseError:
                    pass
                else:
                    raise AssertionError("denied enqueue did not escape")
                db.connection.set_authorizer(None)
                assert db.connection.execute(
                    "SELECT COUNT(*) FROM jobs_v1").fetchone()[0] == 0
                assert db.connection.execute(
                    "SELECT COUNT(*) FROM sqlite_master WHERE name='job_parameters_v1'"
                ).fetchone()[0] == 0
                job = jobs.enqueue("id", {"rows": rows})
                assert jobs.status(job)["state"] == "queued"
            # Write refusal during publication: rollback, retry, reopen.
            with HookDatabase(path) as db:
                jobs = Jobs(db)
                db.connection.set_authorizer(deny(
                    "jobs_v1", (sqlite3.SQLITE_UPDATE,)))
                try:
                    jobs.run(job)
                except sqlite3.DatabaseError:
                    pass
                else:
                    raise AssertionError("denied run did not escape")
                db.connection.set_authorizer(None)
                assert jobs.status(job)["state"] == "queued"
                assert outcome(lambda: jobs.result(job)) == (
                    "failed", "pending", "job has not completed")
                raw = db.connection.execute(
                    "SELECT output,error FROM jobs_v1 WHERE id=?", (job,)).fetchone()
                assert tuple(raw) == (None, None)
                assert temp_tables(db.connection) == []
            # Pre-commit failure: task stays queued, retry succeeds.
            with HookDatabase(path) as db:
                jobs = Jobs(db)
                db.armed = True
                try:
                    jobs.run(job)
                except RuntimeError:
                    pass
                else:
                    raise AssertionError("pre-commit failure did not escape")
                assert jobs.status(job)["state"] == "queued"
                assert temp_tables(db.connection) == []
                assert jobs.run(job) == "done"
                assert jobs.result(job) == rows
            # A late business error is published and leaves no staging.
            with HookDatabase(path) as db:
                catalog, jobs = Catalog(db), Jobs(db)
                catalog.save("late", P(["rows"], M(
                    V("rows"), "x", I(V("x"), V("x"), V("late_error")))))
                late = jobs.enqueue("late", {"rows": [1] * 60001 + [0]})
                assert jobs.run(late) == "failed"
                assert outcome(lambda: jobs.result(late)) == (
                    "failed", "unbound", "late_error")
                assert temp_tables(db.connection) == []
                assert db.connection.execute(
                    "SELECT output FROM jobs_v1 WHERE id=?", (late,)).fetchone()[0] is None
                completed = jobs.enqueue("late", {"rows": [1] * 60000})
                assert jobs.run(completed) == "done"
                assert jobs.result(completed) == [1] * 60000
        finally:
            os.chdir(previous)
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            tempfile.tempdir = saved_tempdir
        assert set(root.iterdir()) == {path}, sorted(p.name for p in root.iterdir())
    say("exception_paths enqueue/run/commit/business ok")


def handle_leaks():
    with tempfile.TemporaryDirectory(prefix="run-checks-fds-") as directory:
        path = Path(directory) / "fds.sqlite"
        with Database(path) as db:
            catalog, jobs = Catalog(db), Jobs(db)
            catalog.save("id", P(["rows"], V("rows")))
            sizes = [5000, 60000, 5000]
            jobs_by_size = [jobs.enqueue("id", {"rows": canonical_rows(size)})
                            for size in sizes]
            baseline = open_files()
            for job in jobs_by_size:
                assert jobs.run(job) == "done"
                assert len(jobs.result(job)) > 0
            after = open_files()
            assert after <= baseline + 1, (baseline, after)
            assert temp_tables(db.connection) == []
            # Repeated terminal runs neither rewrite nor stage.
            row = tuple(db.connection.execute(
                "SELECT * FROM jobs_v1 WHERE id=?", (jobs_by_size[0],)).fetchone())
            assert jobs.run(jobs_by_size[0]) == "done"
            assert tuple(db.connection.execute(
                "SELECT * FROM jobs_v1 WHERE id=?", (jobs_by_size[0],)).fetchone()) == row
            assert temp_tables(db.connection) == []
    say("handle_and_terminal_rerun ok")


# --------------------------------------------------------------------------
# 4. Frozen/disc database compatibility.
# --------------------------------------------------------------------------

def legacy_database():
    with tempfile.TemporaryDirectory(prefix="run-checks-legacy-") as directory:
        path = Path(directory) / "frozen.sqlite"
        create_frozen(path)
        expected = {1: ("done", [9, 9, None]),
                    2: ("done", [[37, 57], [], [None, 37]]),
                    3: ("done", []),
                    4: ("failed", "unbound", "missing"),
                    5: ("done", 5),
                    6: ("done", None)}
        with Database(path) as db:
            catalog, jobs = Catalog(db), Jobs(db)
            assert catalog.revision("offset") == 2
            assert jobs.status(1)["revision"] == 1
            assert jobs.result(3) == []
            assert jobs.result(6) is None
            for job_id in (1, 2, 5):
                assert jobs.status(job_id)["state"] == "queued"
                assert jobs.run(job_id) == "done"
            assert jobs.run(4) == "failed"
            for job_id, want in expected.items():
                if job_id in (3, 6):
                    got = ("done", jobs.result(job_id))
                elif job_id == 4:
                    got = outcome(lambda job_id=job_id: jobs.result(job_id))
                else:
                    got = ("done", jobs.result(job_id))
                assert got == want, (job_id, got, want)
            # Historical rows used the plain text representation.
            assert db.connection.execute(
                "SELECT typeof(parameters) FROM jobs_v1 WHERE id=1").fetchone()[0] == "text"
            assert db.connection.execute("PRAGMA foreign_key_check").fetchall() == []
        with Database(path) as db:
            assert Jobs(db).result(2) == [[37, 57], [], [None, 37]]
    say("legacy_jobs_1_through_6 ok")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.parse_args()
    differential()
    representation()
    exception_paths()
    handle_leaks()
    legacy_database()
    print(json.dumps({"run_checks": "ok", "sections": [
        "differential", "representation", "exception_paths",
        "handle_leaks", "legacy"]}), flush=True)


if __name__ == "__main__":
    main()
