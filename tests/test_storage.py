import json
import sqlite3
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from catalog import Catalog
from jobs import Jobs
from samples import A, C, I, P, V, create_frozen, offset
from storage import Database
from values import WarehouseError


class MemoryStorageTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.catalog, self.jobs = Catalog(self.db), Jobs(self.db)

    def tearDown(self):
        self.db.close()

    def test_revision_and_parameter_snapshots(self):
        plan = offset()
        self.assertEqual(self.catalog.save("p", plan), 1)
        args = {"rows": [[1]], "base": 7}
        # Use valid scalar elements for execution, but test nested data ownership too.
        self.catalog.save("identity", P(["rows"], V("rows")))
        identity = self.jobs.enqueue("identity", {"rows": args["rows"]})
        args["rows"][0][0] = 999
        plan["body"] = C(999)
        self.assertEqual(self.catalog.get("p")["body"]["op"], "map")
        parameters = {"rows": [2, None, 2], "base": 7}
        old = self.jobs.enqueue("p", parameters)
        parameters["rows"][0] = 1000
        parameters["base"] = 1000
        self.assertEqual(self.catalog.save("p", P(["rows", "base"], C(42))), 2)
        fresh = self.jobs.enqueue("p", {"rows": [], "base": 0})
        self.assertEqual(self.jobs.status(old)["revision"], 1)
        self.assertEqual(self.jobs.status(fresh)["revision"], 2)
        for job in (old, fresh, identity):
            self.assertEqual(self.jobs.run(job), "done")
        self.assertEqual(self.jobs.result(old), [9, None, 9])
        self.assertEqual(self.jobs.result(fresh), 42)
        self.assertEqual(self.jobs.result(identity), [[1]])
        self.assertEqual(self.catalog.get("p", 1)["body"]["op"], "map")
        value = self.jobs.result(old)
        value[0] = -1
        self.assertEqual(self.jobs.result(old), [9, None, 9])

    def test_pending_errors_and_idempotent_terminal_runs(self):
        self.catalog.save("p", P([], V("missing")))
        job = self.jobs.enqueue("p", {})
        with self.assertRaises(WarehouseError) as caught:
            self.jobs.result(job)
        self.assertEqual(caught.exception.code, "pending")
        self.assertEqual(self.jobs.run(job), "failed")
        before = tuple(self.db.connection.execute("SELECT * FROM jobs_v1").fetchone())
        self.assertEqual(self.jobs.run(job), "failed")
        self.assertEqual(tuple(self.db.connection.execute("SELECT * FROM jobs_v1").fetchone()),
                         before)
        with self.assertRaises(WarehouseError) as caught:
            self.jobs.result(job)
        self.assertEqual((caught.exception.code, caught.exception.message), ("unbound", "missing"))
        self.catalog.save("p", P([], C(None)))
        job = self.jobs.enqueue("p", {})
        self.assertEqual(self.jobs.run(job), "done")
        self.assertEqual(self.jobs.run(job), "done")
        self.assertIsNone(self.jobs.result(job))
        row = self.db.connection.execute("SELECT output,error FROM jobs_v1 WHERE id=?", (job,)).fetchone()
        self.assertEqual(tuple(row), ("null", None))

    def test_catalog_returns_independent_documents(self):
        self.catalog.save("p", offset())
        document = self.catalog.get("p")
        document["parameters"].append("extra")
        document["body"]["array"] = C(1)
        self.assertEqual(self.catalog.get("p"), offset())

    def test_bad_acceptance_has_no_partial_records(self):
        self.catalog.save("p", offset())
        for parameters in ({}, {"rows": [True], "base": 7}, {"rows": [], "base": 1.5}):
            with self.assertRaises(WarehouseError):
                self.jobs.enqueue("p", parameters)
        self.assertEqual(self.db.connection.execute("SELECT COUNT(*) FROM jobs_v1").fetchone()[0], 0)
        bad = P([], {"op": "invalid"})
        with self.assertRaises(WarehouseError):
            self.catalog.save("p", bad)
        self.assertEqual(self.catalog.revision("p"), 1)

    def test_lazy_runtime_failure_is_not_acceptance_failure(self):
        self.catalog.save("p", P(["gate"], I(V("gate"), C([7]), V("missing"))))
        good = self.jobs.enqueue("p", {"gate": 1})
        bad = self.jobs.enqueue("p", {"gate": 0})
        self.assertEqual(self.jobs.run(good), "done")
        self.assertEqual(self.jobs.result(good), [7])
        self.assertEqual(self.jobs.run(bad), "failed")

    def test_transaction_rolls_back_ddl_bindings_and_version(self):
        self.catalog.save("p", P([], C(1)))
        job = self.jobs.enqueue("p", {})
        def stop(connection):
            self.assertEqual(connection.execute("SELECT version FROM meta").fetchone()[0], 2)
            raise RuntimeError("before commit")
        with self.assertRaisesRegex(RuntimeError, "before commit"):
            with self.db.transaction(before_commit=stop) as connection:
                connection.execute("CREATE TABLE provisional (value TEXT)")
                connection.execute("UPDATE meta SET version=2")
                connection.execute("UPDATE jobs_v1 SET parameters='[]'")
        self.assertEqual(self.db.version, 1)
        self.assertIsNone(self.db.connection.execute(
            "SELECT name FROM sqlite_master WHERE name='provisional'").fetchone())
        self.assertEqual(self.db.connection.execute("SELECT parameters FROM jobs_v1").fetchone()[0], "{}")
        self.assertEqual(self.jobs.run(job), "done")
        self.assertEqual(self.jobs.result(job), 1)

    def test_cancelled_transaction_rolls_back(self):
        class Cancel(BaseException):
            pass
        with self.assertRaises(Cancel):
            with self.db.transaction() as connection:
                connection.execute("CREATE TABLE cancelled (x INTEGER)")
                raise Cancel()
        self.assertIsNone(self.db.connection.execute(
            "SELECT name FROM sqlite_master WHERE name='cancelled'").fetchone())

    def test_foreign_keys_and_missing_entities(self):
        with self.assertRaises(sqlite3.IntegrityError):
            with self.db.transaction() as connection:
                connection.execute(
                    "INSERT INTO jobs_v1(name,revision,parameters,state) VALUES ('absent',1,'{}','queued')")
        for operation in (lambda: self.catalog.get("absent"), lambda: self.jobs.run(123),
                          lambda: self.jobs.result(123), lambda: self.jobs.status(123)):
            with self.assertRaises(WarehouseError) as caught:
                operation()
            self.assertEqual(caught.exception.code, "not_found")


class DiskStorageTests(unittest.TestCase):
    def test_frozen_reopen_update_and_history(self):
        with TemporaryDirectory(prefix="arraywarehouse-test-") as directory:
            path = Path(directory) / "old.sqlite"
            create_frozen(path)
            with Database(path) as db:
                catalog, jobs = Catalog(db), Jobs(db)
                self.assertEqual(catalog.revision("offset"), 2)
                self.assertEqual(catalog.get("offset", 1), offset())
                self.assertEqual(jobs.status(1)["revision"], 1)
                catalog.save("offset", P(["rows", "base"], C(1001)))
                fresh = jobs.enqueue("offset", {"rows": [], "base": 0})
            with Database(path) as db:
                jobs = Jobs(db)
                for job in (1, 2, 5, fresh):
                    self.assertEqual(jobs.run(job), "done")
                self.assertEqual(jobs.result(1), [9, 9, None])
                self.assertEqual(jobs.result(2), [[37, 57], [], [None, 37]])
                self.assertEqual(jobs.result(3), [])
                self.assertEqual(jobs.result(5), 5)
                self.assertIsNone(jobs.result(6))
                self.assertEqual(jobs.result(fresh), 1001)
                with self.assertRaises(WarehouseError) as caught:
                    jobs.result(4)
                self.assertEqual((caught.exception.code, caught.exception.message),
                                 ("unbound", "missing"))
                raw = db.connection.execute("SELECT parameters FROM jobs_v1 WHERE id=1").fetchone()[0]
                self.assertEqual(json.loads(raw), {"rows": [2, 2, None], "base": 7})
                self.assertEqual(db.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
            with Database(path) as db:
                self.assertEqual(Jobs(db).run(1), "done")
                self.assertEqual(Jobs(db).result(1), [9, 9, None])
                with self.assertRaises(ValueError):
                    create_frozen(path)
                self.assertEqual(Jobs(db).result(1), [9, 9, None])

    def test_before_commit_failure_reopen_and_retry(self):
        with TemporaryDirectory(prefix="arraywarehouse-test-") as directory:
            path = Path(directory) / "old.sqlite"
            create_frozen(path)
            with Database(path) as db:
                def stop(connection):
                    raise RuntimeError("commit hook failed")
                with self.assertRaisesRegex(RuntimeError, "commit hook failed"):
                    with db.transaction(before_commit=stop) as connection:
                        connection.execute("CREATE TABLE probe (value INTEGER)")
                        connection.execute("INSERT INTO probe VALUES (7)")
                        connection.execute("UPDATE meta SET version=2")
                        connection.execute("UPDATE jobs_v1 SET parameters='[]' WHERE id=1")
            with Database(path) as db:
                self.assertEqual(db.version, 1)
                self.assertIsNone(db.connection.execute(
                    "SELECT name FROM sqlite_master WHERE name='probe'").fetchone())
                self.assertEqual(Jobs(db).status(1)["state"], "queued")
                with db.transaction() as connection:
                    connection.execute("CREATE TABLE probe (value INTEGER)")
                    connection.execute("INSERT INTO probe VALUES (7)")
                self.assertEqual(Jobs(db).run(1), "done")
                self.assertEqual(Jobs(db).result(1), [9, 9, None])
            with Database(path) as db:
                self.assertEqual(db.connection.execute("SELECT value FROM probe").fetchone()[0], 7)


if __name__ == "__main__":
    unittest.main()
