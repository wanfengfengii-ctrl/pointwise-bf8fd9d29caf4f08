"""Frozen v1 storage specification. Kept independent of application execution."""
SCHEMA_V1 = """
CREATE TABLE meta (
    singleton INTEGER PRIMARY KEY CHECK(singleton=1),
    version INTEGER NOT NULL CHECK(version>=1)
);
INSERT INTO meta VALUES (1,1);
CREATE TABLE plans_v1 (
    name TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision>0),
    payload TEXT NOT NULL,
    PRIMARY KEY(name,revision)
);
CREATE TABLE heads_v1 (
    name TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    FOREIGN KEY(name,revision) REFERENCES plans_v1(name,revision)
);
CREATE TABLE jobs_v1 (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    revision INTEGER NOT NULL,
    parameters TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','done','failed')),
    output TEXT,
    error TEXT,
    FOREIGN KEY(name,revision) REFERENCES plans_v1(name,revision),
    CHECK (
        (state='queued' AND output IS NULL AND error IS NULL) OR
        (state='done' AND output IS NOT NULL AND error IS NULL) OR
        (state='failed' AND output IS NULL AND error IS NOT NULL)
    )
);
"""


def install(connection):
    """Execute only fixed DDL; caller owns the transaction."""
    for statement in SCHEMA_V1.split(";"):
        if statement.strip():
            connection.execute(statement)
