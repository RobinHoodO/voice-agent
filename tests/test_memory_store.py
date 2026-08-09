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
