"""Immutable plan revisions and a separately updated current head."""
import json
import sdk1
from values import dumps, fail, name as valid_name


def lookup(connection, name, revision=None):
    if revision is None:
        row = connection.execute(
            "SELECT p.* FROM plans_v1 p JOIN heads_v1 h "
            "ON p.name=h.name AND p.revision=h.revision WHERE h.name=?", (name,)
        ).fetchone()
    else:
        row = connection.execute(
            "SELECT * FROM plans_v1 WHERE name=? AND revision=?", (name, revision)
        ).fetchone()
    if row is None:
        fail("not_found", "plan revision does not exist")
    return row


class Catalog:
    def __init__(self, database):
        self.database = database

    def save(self, name, plan):
        valid_name(name)
        sdk1.validate(plan)
        payload = dumps(plan)
        with self.database.transaction() as connection:
            self.database.require_v1()
            row = connection.execute(
                "SELECT COALESCE(MAX(revision),0)+1 FROM plans_v1 WHERE name=?", (name,)
            ).fetchone()
            revision = row[0]
            connection.execute("INSERT INTO plans_v1 VALUES (?,?,?)", (name, revision, payload))
            connection.execute(
                "INSERT INTO heads_v1 VALUES (?,?) ON CONFLICT(name) "
                "DO UPDATE SET revision=excluded.revision", (name, revision)
            )
        return revision

    def get(self, name, revision=None):
        self.database.require_v1()
        return json.loads(lookup(self.database.connection, name, revision)["payload"])

    def revision(self, name):
        self.database.require_v1()
        return lookup(self.database.connection, name)["revision"]
