"""Bounded-memory SDK1 execution used by Jobs.run.

Business semantics are exactly those of the frozen ``sdk1`` reference; only the
run-time representation of large arrays changes.  During evaluation a value is

* ``None`` or an exact Python ``int``,
* a plain Python list -- a fully materialised small array, or
* :class:`Spilled` -- an immutable, re-iterable array whose elements live as
  JSON text chunks in a connection-local SQLite TEMP table.

Parameters are parsed straight from the stored JSON blob in bounded pieces,
map/fold results spill after a small budget, and the final document is staged
chunk by chunk.  Staged TEMP tables are dropped on every exit path (success,
business failure, ordinary read/write failure, pre-commit failure), and a
``done`` row is only published after the complete output text has been written
and read back from the row.
"""
import codecs
import json
import sqlite3

import sdk1
from values import WarehouseError, clone, fail

READ_BYTES = 1 << 16          # parameter blob read size
BUILD_CHARS = 1 << 18         # chars per staged chunk row
SMALL_ELEMENTS = 2048         # plain-list budget before spilling
SMALL_CHARS = 1 << 20         # plain-list character budget before spilling
SMALL_OUTPUT = 1 << 20        # documents at or below this are written as text
SPILL_SNAPSHOT = 1 << 20      # larger parameter snapshots use the companion table
EXTERNAL_KEY = "$stored"      # marker in jobs_v1.parameters for a large snapshot
WRITE_FLUSH = 1 << 16         # chars held before writing the output blob
WRITE_SLICE = 1 << 16         # bytes per incremental blob write/read
STREAM_CACHE_PAGES = 64       # page cache during bulk streaming writes/reads

def malformed():
    """Stored snapshot is not JSON: a non-business storage failure."""
    raise ValueError("malformed parameter snapshot")


class Spilled:
    """An immutable complete array whose elements live in a TEMP chunk table."""

    __slots__ = ("session", "table", "length", "json_length")

    def __init__(self, session, table, length, json_length):
        self.session = session
        self.table = table
        self.length = length
        self.json_length = json_length

    def __iter__(self):
        return self.session.scan(self.table)

    def __len__(self):
        return self.length


class Session:
    """Owns the TEMP tables and temp-store setting for one Jobs.run call."""

    def __init__(self, connection):
        self.connection = connection
        self.tables = []
        self.counter = 0
        try:
            self.saved_temp_store = connection.execute("PRAGMA temp_store").fetchone()[0]
        except sqlite3.Error:
            self.saved_temp_store = None
        # Staged chunks must not compete with Python for heap memory.
        try:
            connection.execute("PRAGMA temp_store=FILE")
        except sqlite3.Error:
            self.saved_temp_store = None

    def new_table(self):
        while True:
            self.counter += 1
            name = "run_mem_stage_%d" % self.counter
            row = self.connection.execute(
                "SELECT 1 FROM sqlite_temp_master WHERE type='table' AND name=?",
                (name,),
            ).fetchone()
            if row is None:
                break
        self.connection.execute(
            'CREATE TEMP TABLE "%s" (seq INTEGER PRIMARY KEY, data TEXT NOT NULL)' % name
        )
        self.tables.append(name)
        return name

    def put(self, table, seq, payload):
        self.connection.execute(
            'INSERT INTO "%s"(seq,data) VALUES (?,?)' % table, (seq, payload)
        )

    def scan(self, table):
        cursor = self.connection.execute(
            'SELECT data FROM "%s" ORDER BY seq' % table
        )
        try:
            for row in cursor:
                for value in json.loads(row[0]):
                    yield value
        finally:
            cursor.close()

    def close(self):
        """Drop every staged table; safe after commit and after rollback."""
        for name in reversed(self.tables):
            try:
                self.connection.execute('DROP TABLE IF EXISTS temp."%s"' % name)
            except sqlite3.Error:
                pass
        self.tables = []
        if self.saved_temp_store is not None:
            try:
                self.connection.execute(
                    "PRAGMA temp_store=%d" % int(self.saved_temp_store)
                )
            except sqlite3.Error:
                pass
            self.saved_temp_store = None


def encoded_length(value):
    if value is None:
        return 4
    if type(value) is int:
        return len(str(value))
    if type(value) is list:
        total = 2 + max(0, len(value) - 1)
        for item in value:
            total += encoded_length(item)
        return total
    if isinstance(value, Spilled):
        return value.json_length
    fail("invalid_value", "expected null, integer, or array")


def encode_parts(value):
    if value is None:
        yield "null"
    elif type(value) is int:
        yield str(value)
    elif type(value) is list or isinstance(value, Spilled):
        yield "["
        first = True
        for item in value:
            if not first:
                yield ","
            first = False
            yield from encode_parts(item)
        yield "]"
    else:
        fail("invalid_value", "expected null, integer, or array")


def encoded_text(value):
    if type(value) is int:
        return str(value)
    if value is None:
        return "null"
    return "".join(encode_parts(value))


class ArrayBuilder:
    """Collects map/parameter elements, spilling past a small budget."""

    def __init__(self, session):
        self.session = session
        self.items = []
        self.count = 0
        self.chars = 0
        self.table = None
        self.seq = 0
        self.pending = []
        self.pending_chars = 0

    def add(self, value):
        self.count += 1
        separator = 1 if self.count > 1 else 0
        if self.table is None:
            self.items.append(value)
            self.chars += encoded_length(value) + separator
            if len(self.items) > SMALL_ELEMENTS or self.chars + 2 > SMALL_CHARS:
                self._start()
        else:
            text = encoded_text(value)
            self.chars += len(text) + separator
            self._append(text)

    def _start(self):
        self.table = self.session.new_table()
        items, self.items = self.items, []
        for value in items:
            self._append(encoded_text(value))

    def _append(self, text):
        self.pending.append(text)
        self.pending_chars += len(text) + 1
        if self.pending_chars >= BUILD_CHARS:
            self._flush()

    def _flush(self):
        if not self.pending:
            return
        payload = "[" + ",".join(self.pending) + "]"
        self.session.put(self.table, self.seq, payload)
        self.seq += 1
        self.pending = []
        self.pending_chars = 0

    def finish(self):
        if self.table is None:
            items, self.items = self.items, []
            return items
        self._flush()
        return Spilled(self.session, self.table, self.count, 2 + self.chars)


def sha256():
    """Import hashlib on first use only.

    ``hashlib`` pulls in OpenSSL, several megabytes of resident memory.  The
    enqueue path never needs a digest, so keeping the import out of module load
    keeps the accepting process small.
    """
    global _sha256
    if _sha256 is None:
        import hashlib
        _sha256 = hashlib.sha256
    return _sha256()


_sha256 = None


class OutputSink:
    """Streams the final document into the destination row's text blob.

    The exact length is known from the value's representation before any byte
    is written, so the output is never staged a second time.
    """

    def __init__(self, connection, job_id, total):
        self.connection = connection
        self.job_id = job_id
        self.total = total
        self.parts = []
        self.pending = 0
        self.written = 0
        self.digest = sha256()

    def start(self):
        # A plain zeroblob is allocated in place; CAST(... AS TEXT) would first
        # materialise the whole value and spike process memory.
        self.connection.execute(
            "UPDATE jobs_v1 SET state='done', output=zeroblob(?) WHERE id=?",
            (self.total, self.job_id),
        )
        self.blob = self.connection.blobopen("jobs_v1", "output", self.job_id)

    def write(self, text):
        self.parts.append(text)
        self.pending += len(text)
        if self.pending >= WRITE_FLUSH:
            self._flush()

    def _flush(self):
        if not self.parts:
            return
        raw = "".join(self.parts).encode("utf-8")
        self.parts = []
        self.pending = 0
        for offset in range(0, len(raw), WRITE_SLICE):
            piece = raw[offset:offset + WRITE_SLICE]
            self.blob.write(piece)
            self.digest.update(piece)
            self.written += len(piece)

    def finish(self):
        self._flush()
        self.blob.close()

    def discard(self):
        self.blob.close()


class Feed:
    """Chunked UTF-8 JSON feed over the stored parameter blob."""

    def __init__(self, blob):
        self.blob = blob
        self.decoder = codecs.getincrementaldecoder("utf-8")()
        self.text = ""
        self.pos = 0
        self.eof = False
        self.json = json.JSONDecoder()

    def more(self):
        """Append the next decoded piece; return whether any text was added."""
        while not self.eof:
            raw = self.blob.read(READ_BYTES)
            if not raw:
                self.eof = True
                tail = self.decoder.decode(b"", final=True)
                if tail:
                    self.text += tail
                    return True
                return False
            piece = self.decoder.decode(raw)
            if piece:
                self.text += piece
                return True
        return False

    def compact(self):
        if self.pos >= READ_BYTES:
            self.text = self.text[self.pos:]
            self.pos = 0

    def skip_ws(self):
        while True:
            text = self.text
            pos = self.pos
            size = len(text)
            while pos < size and text[pos] in " \t\r\n":
                pos += 1
            self.pos = pos
            if pos < size:
                return
            if not self.more():
                return

    def peek(self):
        self.skip_ws()
        if self.pos >= len(self.text):
            return ""
        return self.text[self.pos]

    def expect(self, char):
        if self.peek() != char:
            malformed()
        self.pos += 1

    def decode(self):
        """Decode one complete JSON value, refilling across chunk edges."""
        self.skip_ws()
        while True:
            try:
                value, end = self.json.raw_decode(self.text, self.pos)
            except json.JSONDecodeError:
                if self.more():
                    continue
                malformed()
            if end >= len(self.text) and not self.eof and self.more():
                continue
            self.pos = end
            return value

    def checked(self):
        value = self.decode()
        business(value)
        return value

    def at_end(self):
        while True:
            self.skip_ws()
            if self.pos < len(self.text):
                return False
            if self.eof or not self.more():
                return True


def business(value):
    """The stored parameter value must be null, integer or nested arrays."""
    if value is None or type(value) is int:
        return
    if type(value) is list:
        for item in value:
            business(item)
        return
    fail("invalid_value", "expected null, integer, or array")


def read_parameters(session, job_id, names):
    """Parse the stored snapshot without materialising whole arrays."""
    blob = parameter_blob(session.connection, job_id)
    try:
        feed = Feed(blob)
        if feed.peek() != "{":
            feed.skip_ws()
            if feed.peek() == "" and feed.at_end():
                malformed()
            fail("invalid_params",
                 "parameters must match the declared names exactly")
        feed.pos += 1
        env = {}
        if feed.peek() == "}":
            feed.pos += 1
        else:
            while True:
                if feed.peek() != '"':
                    malformed()
                key = feed.decode()
                if type(key) is not str:
                    malformed()
                feed.skip_ws()
                feed.expect(":")
                if feed.peek() == "[":
                    feed.pos += 1
                    builder = ArrayBuilder(session)
                    if feed.peek() == "]":
                        feed.pos += 1
                    else:
                        while True:
                            builder.add(feed.checked())
                            feed.compact()
                            if feed.peek() == ",":
                                feed.pos += 1
                                continue
                            if feed.peek() == "]":
                                feed.pos += 1
                                break
                            malformed()
                    env[key] = builder.finish()
                else:
                    env[key] = feed.checked()
                feed.compact()
                if feed.peek() == ",":
                    feed.pos += 1
                    continue
                if feed.peek() == "}":
                    feed.pos += 1
                    break
                malformed()
        if not feed.at_end():
            malformed()
        if set(env) != set(names):
            fail("invalid_params",
                 "parameters must match the declared names exactly")
        return env
    finally:
        blob.close()


def plus(left, right):
    for value in (left, right):
        if isinstance(value, Spilled) or (value is not None and type(value) is not int):
            fail("type", "addition expects nullable integers")
    return None if left is None or right is None else left + right


def truth(value):
    if isinstance(value, Spilled) or (value is not None and type(value) is not int):
        fail("type", "condition expects a nullable integer")
    return value is not None and value != 0


def as_array(value):
    if isinstance(value, Spilled):
        return value
    if value is not None and type(value) is not list:
        fail("type", "array operation expects an array or null")
    return value


def evaluate(node, env, session):
    op = node["op"]
    if op == "const":
        return clone(node["value"])
    if op == "var":
        key = node["name"]
        if key not in env:
            fail("unbound", key)
        return env[key]
    if op == "add":
        # Both sides are fully evaluated before the type check, as in sdk1.
        left = evaluate(node["left"], env, session)
        right = evaluate(node["right"], env, session)
        return plus(left, right)
    if op == "if":
        branch = "then" if truth(evaluate(node["test"], env, session)) else "else"
        return evaluate(node[branch], env, session)
    if op == "map":
        source = as_array(evaluate(node["array"], env, session))
        if source is None:
            return None
        builder = ArrayBuilder(session)
        for item in source:
            local = dict(env)
            local[node["param"]] = item
            builder.add(evaluate(node["body"], local, session))
        return builder.finish()
    # fold: the complete source is a value before initial is evaluated.
    source = as_array(evaluate(node["array"], env, session))
    if source is None:
        return None
    acc = evaluate(node["initial"], env, session)
    for item in source:
        local = dict(env)
        local[node["acc"]] = acc
        local[node["item"]] = item
        acc = evaluate(node["body"], local, session)
    return acc


def check_bind(document, parameters):
    """Validation half of ``sdk1.bind`` without copying the values."""
    if type(parameters) is not dict or set(parameters) != set(document["parameters"]):
        fail("invalid_params", "parameters must match the declared names exactly")
    for value in parameters.values():
        business(value)


def snapshot_length(document, parameters):
    """Exact JSON byte length of the snapshot ``sdk1.bind`` would store."""
    total = 2
    for index, key in enumerate(sorted(document["parameters"])):
        if index:
            total += 1
        total += len(json.dumps(key, ensure_ascii=False).encode("utf-8")) + 1
        total += encoded_length(parameters[key])
    return total


def snapshot_parts(document, parameters):
    yield "{"
    for index, key in enumerate(sorted(document["parameters"])):
        if index:
            yield ","
        yield json.dumps(key, ensure_ascii=False)
        yield ":"
        yield from encode_parts(parameters[key])
    yield "}"


PARAMETER_TABLE = "job_parameters_v1"


def bounded_cache(connection):
    """Temporarily cap SQLite's page cache for a bulk streaming write/read."""
    saved = connection.execute("PRAGMA cache_size").fetchone()[0]
    try:
        connection.execute("PRAGMA cache_size=%d" % STREAM_CACHE_PAGES)
    except sqlite3.Error:
        return None
    return saved


def restore_cache(connection, saved):
    if saved is not None:
        try:
            connection.execute("PRAGMA cache_size=%d" % int(saved))
        except sqlite3.Error:
            pass


def ensure_parameter_table(connection):
    """Companion table for large snapshots.

    ``jobs_v1.parameters`` is a mid-record TEXT column: SQLite materialises a
    whole zeroblob there instead of allocating it in place, which spikes the
    writer's heap.  This table keeps the payload as its last column so the
    row can be allocated and filled incrementally.  Rows are keyed by job id
    and stay inside the same transaction/database file.
    """
    connection.execute(
        "CREATE TABLE IF NOT EXISTS %s ("
        " job_id INTEGER PRIMARY KEY REFERENCES jobs_v1(id),"
        " data BLOB NOT NULL)" % PARAMETER_TABLE
    )


def store_snapshot(connection, name, revision, document, parameters):
    """Insert a queued row whose parameter snapshot is an exact JSON copy.

    Small snapshots take the ordinary ``jobs_v1.parameters`` text path, so any
    reader sees them where the frozen schema puts them.  Large snapshots are
    written to the companion table as one BLOB row and ``jobs_v1.parameters``
    holds a small marker document instead; this keeps the accept path from
    building a second full copy of the tree.
    """
    total = snapshot_length(document, parameters)
    if total <= SPILL_SNAPSHOT:
        text = "".join(snapshot_parts(document, parameters))
        if len(text.encode("utf-8")) != total:
            raise RuntimeError("snapshot length mismatch")
        cursor = connection.execute(
            "INSERT INTO jobs_v1(name,revision,parameters,state) VALUES (?,?,?,'queued')",
            (name, revision, text),
        )
        return cursor.lastrowid
    ensure_parameter_table(connection)
    marker = json.dumps({EXTERNAL_KEY: total}, separators=(",", ":"))
    cursor = connection.execute(
        "INSERT INTO jobs_v1(name,revision,parameters,state) VALUES (?,?,?,'queued')",
        (name, revision, marker),
    )
    job_id = cursor.lastrowid
    saved = bounded_cache(connection)
    try:
        connection.execute(
            "INSERT INTO %s(job_id,data) VALUES (?,zeroblob(?))" % PARAMETER_TABLE,
            (job_id, total),
        )
        blob = connection.blobopen(PARAMETER_TABLE, "data", job_id)
        written = 0
        try:
            pending = []
            pending_chars = 0
            for part in snapshot_parts(document, parameters):
                pending.append(part)
                pending_chars += len(part)
                if pending_chars >= WRITE_FLUSH:
                    raw = "".join(pending).encode("utf-8")
                    pending, pending_chars = [], 0
                    blob.write(raw)
                    written += len(raw)
            if pending:
                raw = "".join(pending).encode("utf-8")
                blob.write(raw)
                written += len(raw)
        finally:
            blob.close()
    finally:
        restore_cache(connection, saved)
    if written != total:
        raise RuntimeError("snapshot length mismatch")
    row = connection.execute(
        "SELECT length(data) FROM %s WHERE job_id=?" % PARAMETER_TABLE, (job_id,)
    ).fetchone()
    if row is None or int(row[0]) != total:
        raise RuntimeError("snapshot verification failed")
    return job_id


def parameter_blob(connection, job_id):
    """Open the stored snapshot, whichever representation it uses.

    The companion row's existence -- not the shape of any marker text -- is
    what decides: a plan may legitimately declare a parameter named like the
    marker, and its small snapshot must stay readable from ``jobs_v1``.  The
    ``parameters`` column itself is never selected here, so a large legacy
    snapshot is streamed rather than materialised as Python text.
    """
    row = connection.execute(
        "SELECT 1 FROM jobs_v1 WHERE id=?", (job_id,)
    ).fetchone()
    if row is None:
        fail("not_found", "job does not exist")
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (PARAMETER_TABLE,),
    ).fetchone()
    if table is not None:
        stored = connection.execute(
            "SELECT 1 FROM %s WHERE job_id=?" % PARAMETER_TABLE, (job_id,)
        ).fetchone()
        if stored is not None:
            return connection.blobopen(PARAMETER_TABLE, "data", job_id,
                                       readonly=True)
    return connection.blobopen("jobs_v1", "parameters", job_id, readonly=True)


def execute(job_id, document, session, connection):
    """Validate, parse parameters, evaluate and stage the final document.

    Returns nothing; the destination row is left in state ``done`` with a
    complete, verified ``output`` inside the caller's transaction.  Any error
    propagates after the partially written output blob is closed, so the
    transaction rolls back and the row stays ``queued``.
    """
    sdk1.validate(document)
    env = read_parameters(session, job_id, document["parameters"])
    value = evaluate(document["body"], env, session)
    total = encoded_length(value)
    if total <= SMALL_OUTPUT:
        text = "".join(encode_parts(value))
        if len(text.encode("utf-8")) != total:
            raise RuntimeError("output length mismatch")
        publish_small(connection, job_id, text)
        return
    sink = OutputSink(connection, job_id, total)
    sink.start()
    try:
        for part in encode_parts(value):
            sink.write(part)
        sink.finish()
    except BaseException:
        sink.discard()
        raise
    verify_stream(connection, job_id, sink)


def publish_small(connection, job_id, text):
    connection.execute(
        "UPDATE jobs_v1 SET state='done', output=? WHERE id=?", (text, job_id)
    )
    row = connection.execute(
        "SELECT output, typeof(output) FROM jobs_v1 WHERE id=?", (job_id,)
    ).fetchone()
    if row is None or row[1] != "text" or row[0] != text:
        raise RuntimeError("output verification failed")


def verify_stream(connection, job_id, sink):
    """Read the whole stored document back through a fresh blob handle."""
    if sink.written != sink.total:
        raise RuntimeError("output length mismatch")
    check = sha256()
    counted = 0
    blob = connection.blobopen("jobs_v1", "output", job_id, readonly=True)
    try:
        while True:
            chunk = blob.read(WRITE_SLICE)
            if not chunk:
                break
            check.update(chunk)
            counted += len(chunk)
    finally:
        blob.close()
    if counted != sink.total or check.digest() != sink.digest.digest():
        raise RuntimeError("output verification failed")
    row = connection.execute(
        "SELECT typeof(output), length(CAST(output AS BLOB)) FROM jobs_v1 WHERE id=?",
        (job_id,),
    ).fetchone()
    # Large documents are stored as byte-identical BLOBs so the row can be
    # allocated and written incrementally; sqlite stores the bytes unchanged
    # and ``json.loads`` reads str, bytes or bytearray alike.
    if row is None or row[0] not in ("blob", "text") or int(row[1]) != sink.total:
        raise RuntimeError("output verification failed")
