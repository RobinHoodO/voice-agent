"""Phase 2.5 / 3.3 — memory DB hardening (WAL + busy_timeout, schema once)."""
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


def test_first_writers_race_on_schema_creation(tmp_app):
    """Five writers hitting a brand-new DB at once must all land.

    The test above only caught this by luck of scheduling — it passed alone and failed
    under full-suite load. A barrier makes the window deterministic: every thread is
    inside `_db()` before any of them can publish the path into `_schema_done`, which
    is exactly the moment the unlocked check-then-add used to let a thread skip schema
    creation and INSERT into tables that did not exist yet.
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
