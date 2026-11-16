"""Durable revision/parameter snapshots, eager single-worker execution."""
import json
import stream_exec
from catalog import lookup
from values import WarehouseError, dumps, fail


def job_row(connection, job_id, columns="*"):
    row = connection.execute(
        "SELECT %s FROM jobs_v1 WHERE id=?" % columns, (job_id,)
    ).fetchone()
    if row is None:
        fail("not_found", "job does not exist")
    return row


def job_state(connection, job_id):
    """Read only the small routing columns of a job row.

    The parameter and output columns may hold multi-megabyte documents; a
    ``SELECT *`` would materialise them in the Python heap even though run
    streams them from the row instead.
    """
    row = job_row(connection, job_id, "id,name,revision,state")
    return row


class Jobs:
    def __init__(self, database):
        self.database = database

    def enqueue(self, name, parameters):
        with self.database.transaction() as connection:
            self.database.require_v1()
            plan = lookup(connection, name)
            document = json.loads(plan["payload"])
            stream_exec.check_bind(document, parameters)
            return stream_exec.store_snapshot(
                connection, name, plan["revision"], document, parameters
            )

    def run(self, job_id):
        session = None
        try:
            with self.database.transaction() as connection:
                self.database.require_v1()
                job = job_state(connection, job_id)
                if job["state"] != "queued":
                    return job["state"]
                plan = lookup(connection, job["name"], job["revision"])
                session = stream_exec.Session(connection)
                try:
                    stream_exec.execute(
                        job_id, json.loads(plan["payload"]), session, connection
                    )
                except WarehouseError as error:
                    connection.execute(
                        "UPDATE jobs_v1 SET state='failed',error=? WHERE id=?",
                        (dumps({"code": error.code, "message": error.message}), job_id),
                    )
                    return "failed"
                return "done"
        finally:
            # Drop staged TEMP tables only after the transaction has ended:
            # inside a rolled-back transaction the DDL would be undone too.
            if session is not None:
                session.close()

    def status(self, job_id):
        self.database.require_v1()
        row = job_state(self.database.connection, job_id)
        return {"id": row["id"], "name": row["name"], "revision": row["revision"],
                "state": row["state"]}

    def result(self, job_id):
        self.database.require_v1()
        row = job_row(self.database.connection, job_id, "state,output,error")
        if row["state"] == "queued":
            fail("pending", "job has not completed")
        if row["state"] == "failed":
            error = json.loads(row["error"])
            raise WarehouseError(error["code"], error["message"])
        return json.loads(row["output"])
