"""Phase 2.5 / 3.3 — memory DB hardening (WAL + busy_timeout, schema once)."""
import sqlite3
import threading

from core import memory


def test_wal_enabled(tmp_app):
    conn = memory._db()
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "wal"
    finally:
        conn.close()


def test_concurrent_records_not_dropped(tmp_app):
    # With busy_timeout, concurrent writers wait for the lock instead of the write
    # being silently dropped (returning 0). All inserts should get a real id.
    results = []

    def worker(i):
        results.append(memory.record(f"transcript number {i}", summary=f"s{i}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(results) == 5
    assert all(cid > 0 for cid in results), f"a write was dropped: {results}"


def test_write_survives_a_failed_wal_switch(tmp_app, monkeypatch):
    """A conversation must land even when the DB refuses to switch to WAL.

    This is the actual regression guard, and it is deterministic — no threads, no
    timing. Converting a fresh DB from rollback-journal to WAL takes an EXCLUSIVE
    lock, and `busy_timeout` does not cover journal_mode changes: SQLite returns
    SQLITE_BUSY immediately rather than calling the busy handler. Anything else
    holding the DB therefore makes that pragma raise `database is locked` — on a
    first launch, simply the other thread doing the same setup.

    That error used to propagate out of `_db()`, and `record()` turned it into the
    0 sentinel: the transcript was dropped and the only trace was a log line.
    Forcing the pragma to fail reproduces it exactly, every run.
    """
    def refuse(conn):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(memory, "_enable_wal", refuse)

    cid = memory.record("a conversation worth keeping", summary="s")

    assert cid > 0, "the write was dropped because journal_mode=WAL could not be set"
    conn = memory._db()
    try:
        assert conn.execute(
            "SELECT transcript FROM conversations WHERE id=?", (cid,)
        ).fetchone()[0] == "a conversation worth keeping"
    finally:
        conn.close()


def test_a_declined_wal_switch_is_logged_not_swallowed(tmp_app, monkeypatch):
    """`PRAGMA journal_mode=WAL` can refuse without raising — that must not pass quietly.

    The pragma returns the mode it kept instead of throwing: `'delete'` when a
    transaction is already open, `'memory'` for an in-memory DB. Both verified against
    sqlite directly. Nothing is lost, but `_schema_done` is marked either way, so the
    process never retries and would otherwise believe it had WAL forever.
    """
    logged = []
    monkeypatch.setattr(memory, "_log", logged.append)
    monkeypatch.setattr(memory, "_enable_wal", lambda conn: "delete")

    cid = memory.record("a conversation worth keeping", summary="s")

    assert cid > 0, "a declined WAL switch must not cost the write"
    assert any("delete" in m for m in logged), f"the downgrade went unlogged: {logged}"


def test_concurrent_first_writers_all_land(tmp_app):
    """Five writers hitting a brand-new DB at once must all land.

    Deliberately labelled: this one is a probabilistic smoke test, not the regression
    guard — a barrier releases the threads together but cannot pin which statement
    interleaves with which. Pre-fix it failed roughly half the time, which is exactly
    why `test_write_survives_a_failed_wal_switch` above exists. Kept because it is the
    only test that exercises the real `_schema_lock` under real contention.
    """
    start = threading.Barrier(5)
    results = []

    def worker(i):
        start.wait()
        results.append(memory.record(f"transcript number {i}", summary=f"s{i}"))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(results) == [1, 2, 3, 4, 5], f"a write was dropped: {results}"

    # And the FTS index must agree with the table — a row that made it into
    # `conversations` but not `conversations_fts` is invisible to recall().
    conn = memory._db()
    try:
        assert conn.execute("SELECT count(*) FROM conversations").fetchone()[0] == 5
        assert conn.execute("SELECT count(*) FROM conversations_fts").fetchone()[0] == 5
    finally:
        conn.close()
