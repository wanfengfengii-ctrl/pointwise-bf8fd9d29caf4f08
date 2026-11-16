"""Single-worker SQLite transaction ownership and commit fault seam."""
import sqlite3
from contextlib import contextmanager
from legacy_format import install
from values import fail


class Database:
    def __init__(self, path=":memory:"):
        self.connection = sqlite3.connect(str(path), isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys=ON")
        try:
            with self.transaction() as connection:
                tables = connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
                if not tables:
                    install(connection)
                self.require_v1()
        except BaseException:
            self.close()
            raise

    @property
    def version(self):
        row = self.connection.execute("SELECT version FROM meta WHERE singleton=1").fetchone()
        if row is None:
            fail("storage_version", "missing storage version")
        return row["version"]

    def require_v1(self):
        if self.version != 1:
            fail("storage_version", "this application requires v1 storage")

    @contextmanager
    def transaction(self, before_commit=None):
        """DDL and DML roll back together, including exceptions from the hook."""
        connection = self.connection
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            if before_commit is not None:
                before_commit(connection)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    def close(self):
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
