-- Offline target storage contract; the v1 application does not install this.
-- Program bodies are SDK2 documents. arguments are positional JSON arrays.
-- A migration chooses the parameter_order convention shared by both tables.
CREATE TABLE plans_v2 (
    name TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision>0),
    parameter_order TEXT NOT NULL,
    program TEXT NOT NULL,
    PRIMARY KEY(name,revision)
);
CREATE TABLE heads_v2 (
    name TEXT PRIMARY KEY,
    revision INTEGER NOT NULL,
    FOREIGN KEY(name,revision) REFERENCES plans_v2(name,revision)
);
CREATE TABLE jobs_v2 (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    revision INTEGER NOT NULL,
    arguments TEXT NOT NULL,
    state TEXT NOT NULL CHECK(state IN ('queued','done','failed')),
    output TEXT,
    error TEXT,
    FOREIGN KEY(name,revision) REFERENCES plans_v2(name,revision),
    CHECK (
        (state='queued' AND output IS NULL AND error IS NULL) OR
        (state='done' AND output IS NOT NULL AND error IS NULL) OR
        (state='failed' AND output IS NULL AND error IS NOT NULL)
    )
);
