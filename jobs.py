"""Durable revision/parameter snapshots, eager single-worker execution."""
import json
import sdk1
from catalog import lookup
from values import WarehouseError, dumps, fail


def job_row(connection, job_id):
    row = connection.execute("SELECT * FROM jobs_v1 WHERE id=?", (job_id,)).fetchone()
    if row is None:
        fail("not_found", "job does not exist")
    return row


class Jobs:
    def __init__(self, database):
        self.database = database

    def enqueue(self, name, parameters):
        with self.database.transaction() as connection:
            self.database.require_v1()
            plan = lookup(connection, name)
            snapshot = sdk1.bind(json.loads(plan["payload"]), parameters)
            cursor = connection.execute(
                "INSERT INTO jobs_v1(name,revision,parameters,state) VALUES (?,?,?,'queued')",
                (name, plan["revision"], dumps(snapshot)),
            )
            return cursor.lastrowid

    def run(self, job_id):
        with self.database.transaction() as connection:
            self.database.require_v1()
            job = job_row(connection, job_id)
            if job["state"] != "queued":
                return job["state"]
            plan = lookup(connection, job["name"], job["revision"])
            try:
                output = sdk1.execute(json.loads(plan["payload"]), json.loads(job["parameters"]))
            except WarehouseError as error:
                connection.execute(
                    "UPDATE jobs_v1 SET state='failed',error=? WHERE id=?",
                    (dumps({"code": error.code, "message": error.message}), job_id),
                )
                return "failed"
            connection.execute(
                "UPDATE jobs_v1 SET state='done',output=? WHERE id=?",
                (dumps(output), job_id),
            )
            return "done"

    def status(self, job_id):
        self.database.require_v1()
        row = job_row(self.database.connection, job_id)
        return {"id": row["id"], "name": row["name"], "revision": row["revision"],
                "state": row["state"]}

    def result(self, job_id):
        self.database.require_v1()
        row = job_row(self.database.connection, job_id)
        if row["state"] == "queued":
            fail("pending", "job has not completed")
        if row["state"] == "failed":
            error = json.loads(row["error"])
            raise WarehouseError(error["code"], error["message"])
        return json.loads(row["output"])
