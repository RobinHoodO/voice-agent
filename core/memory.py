"""Conversation memory for Thrivbe Voice.

Two deliberately separated layers:

  • App-owned store (the portable core): a local SQLite DB with an FTS5 index over
    the transcript + summary of every voice conversation. Ships with the app, works
    with zero external dependencies, and is the default on a fresh install.

  • External recall provider (pluggable, read-only): pulls broader context from
    whatever the user already has. `claude-mem` (read-only query of its SQLite store)
    on Robin's machine now; `command` to shell out to a buyer's own knowledge base;
    or `none`. Selected via config key `live.memory.provider`.

`recall()` always searches the app-owned store, then appends the provider's hits.
So the product stands alone, and lights up with claude-mem via one config value.

CLI (so the delegate `pi` can search memory from the workspace shell):
    python memory.py recall "<query>"
    python memory.py recent
"""

import os
import re
import shlex
import sqlite3
import subprocess
import threading
import time

from core import config

# DB_PATH is module-global so demo()/tests can point it at a tempfile.
DB_PATH = os.path.join(config.SUPPORT_DIR, "conversations.db")
_schema_done = set()   # DB paths whose schema + WAL we've already set up (once per path)
# Serialises first-launch setup so two threads don't run the CREATEs — and, more to the
# point, the journal_mode switch below — against each other. The check and the add are
# also made atomic here, which they weren't before.
_schema_lock = threading.Lock()


def _enable_wal(conn: sqlite3.Connection) -> None:
    """Switch the DB to WAL. Best-effort: the caller must treat failure as survivable.

    Converting a database from rollback-journal to WAL takes an EXCLUSIVE lock, and
    `busy_timeout` does NOT cover journal_mode changes — SQLite returns SQLITE_BUSY
    straight away instead of calling the busy handler. So this raises
    `OperationalError('database is locked')` whenever anything else is holding the DB,
    which on a fresh install is simply the other thread doing the same first-launch
    setup. It used to take the whole write down with it: `record()` caught the error and
    returned its 0 sentinel, and the conversation was gone with only a line in the log.

    WAL is a performance choice. The transcript is not. If the switch can't happen now
    it happens on the next launch, and meanwhile the write still lands.
    """
    conn.execute("PRAGMA journal_mode=WAL")


# --- app-owned conversation store -------------------------------------------
def _db() -> sqlite3.Connection:
    config.ensure_dirs()
    conn = sqlite3.connect(DB_PATH, timeout=5.0)
    try:
        os.chmod(DB_PATH, 0o600)   # transcripts + learned PII — owner-only
    except OSError:
        pass
    # Wait for a lock instead of instantly raising "database is locked" — the
    # background _learn thread and the main thread both write.
    conn.execute("PRAGMA busy_timeout=5000")
    if DB_PATH not in _schema_done:
        # Schema + WAL set up once per path, not on every call (was a per-call cost
        # that taxed session-start latency). WAL lets reads proceed during a write.
        # The unlocked check above is the fast path and stays unlocked for that reason;
        # under the lock we check again, because the path may have been finished by
        # another thread while we waited. Set membership and add are atomic under the
        # GIL, so a reader that sees the path always sees a complete schema.
        with _schema_lock:
            if DB_PATH not in _schema_done:
                try:
                    _enable_wal(conn)
                except sqlite3.OperationalError as e:
                    # Survivable by design — see _enable_wal. Losing WAL costs some
                    # read concurrency until the next launch; raising here would cost
                    # the conversation.
                    _log(f"memory: staying on the rollback journal for now ({e})")
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS conversations ("
                    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " ts TEXT NOT NULL, ts_epoch INTEGER NOT NULL,"
                    " summary TEXT, transcript TEXT)")
                # Plain (manually-managed) FTS5 so UPDATEs stay trivial: rowid == conversations.id.
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS conversations_fts "
                    "USING fts5(summary, transcript)")
                # Durable learnings — the continuous-learning layer on top of raw conversations.
                # strength*recency ranks them; corrections supersede instead of deleting.
                # type is preference | fact | correction; status is active | superseded.
                conn.execute(
                    "CREATE TABLE IF NOT EXISTS learnings ("
                    " id INTEGER PRIMARY KEY AUTOINCREMENT,"
                    " created_at TEXT NOT NULL, ts_epoch INTEGER NOT NULL,"
                    " type TEXT NOT NULL,"
                    " text TEXT NOT NULL,"
                    " source_conv_id INTEGER,"
                    " strength REAL NOT NULL DEFAULT 1.0,"
                    " uses INTEGER NOT NULL DEFAULT 0,"
                    " last_used_epoch INTEGER,"
                    " superseded_by INTEGER,"
                    " status TEXT NOT NULL DEFAULT 'active')")
                conn.execute(
                    "CREATE VIRTUAL TABLE IF NOT EXISTS learnings_fts USING fts5(text)")
                # Belt-and-braces, and worth being honest about: sqlite3 runs DDL in
                # autocommit (only DML opens an implicit transaction), so this is a
                # no-op today — `in_transaction` is already False here. It is one cheap
                # line that keeps "schema durable before the path is published" true if
                # isolation_level is ever set, instead of resting on that default.
                conn.commit()
                _schema_done.add(DB_PATH)
    return conn


def record(transcript: str, summary: str = "") -> int:
    """Persist one conversation; returns its id (0 if empty/failure)."""
    transcript = (transcript or "").strip()
    if not transcript:
        return 0
    try:
        conn = _db()
        try:
            cur = conn.execute(
                "INSERT INTO conversations(ts, ts_epoch, summary, transcript) "
                "VALUES (?,?,?,?)",
                (time.strftime("%Y-%m-%d %H:%M"), int(time.time()), summary, transcript))
            cid = cur.lastrowid
            conn.execute(
                "INSERT INTO conversations_fts(rowid, summary, transcript) VALUES (?,?,?)",
                (cid, summary, transcript))
            conn.commit()
            return cid
        finally:
            conn.close()
    except Exception as e:
        _log(f"memory.record failed: {e!r}")
        return 0


# --- crash-safe turn journal ------------------------------------------------
# The transcript used to reach SQLite only in LiveSession._run's `finally`. A SIGSEGV
# doesn't run `finally` — on 2026-08-04 a CoreAudio segfault took a whole conversation
# (an idea Robin had just spent three minutes describing) with it. Every turn now lands
# in a plain append-only file the instant it's spoken; the DB write clears it, and the
# next launch recovers anything still lying around.
JOURNAL_PATH = os.path.join(config.SUPPORT_DIR, "live-turns.journal")


def journal_append(turn: str) -> None:
    """Append one "you: …"/"agent: …" line. Best-effort and unbuffered — worth ~nothing
    if it isn't on disk before the crash it exists to survive."""
    turn = (turn or "").strip()
    if not turn:
        return
    try:
        config.ensure_dirs()
        with open(JOURNAL_PATH, "a", encoding="utf-8") as f:
            f.write(turn.replace("\n", " ") + "\n")
            f.flush()
            os.fsync(f.fileno())
        try:
            os.chmod(JOURNAL_PATH, 0o600)   # same PII as the DB
        except OSError:
            pass
    except Exception as e:
        _log(f"memory.journal_append failed: {e!r}")


def journal_clear() -> None:
    """Drop the journal — the conversation is safely in SQLite now."""
    try:
        os.remove(JOURNAL_PATH)
    except FileNotFoundError:
        pass
    except Exception as e:
        _log(f"memory.journal_clear failed: {e!r}")


def journal_recover() -> int:
    """A leftover journal means the last session died without persisting. Store it as a
    conversation so the words survive, and return its id (0 if there was nothing).
    Called once at launch."""
    try:
        with open(JOURNAL_PATH, encoding="utf-8") as f:
            text = f.read().strip()
    except FileNotFoundError:
        return 0
    except Exception as e:
        _log(f"memory.journal_recover read failed: {e!r}")
        return 0
    if not text:
        journal_clear()
        return 0
    cid = record(text, summary="(recovered after the app closed unexpectedly)")
    if cid:
        journal_clear()
        _log(f"recovered unsaved conversation from journal (id={cid}, "
             f"{len(text.splitlines())} turns)")
    return cid


def set_summary(conv_id: int, summary: str) -> None:
    """Fill in a conversation's summary once it's been generated; keeps FTS in sync."""
    if not conv_id:
        return
    try:
        conn = _db()
        try:
            row = conn.execute(
                "SELECT transcript FROM conversations WHERE id=?", (conv_id,)).fetchone()
            if not row:
                return
            conn.execute("UPDATE conversations SET summary=? WHERE id=?", (summary, conv_id))
            conn.execute("DELETE FROM conversations_fts WHERE rowid=?", (conv_id,))
            conn.execute(
                "INSERT INTO conversations_fts(rowid, summary, transcript) VALUES (?,?,?)",
                (conv_id, summary, row[0]))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        _log(f"memory.set_summary failed: {e!r}")


def recent(k: int = 5) -> str:
    """The most recent k conversation summaries (recency order). Used at session start,
    where there's no query yet. Falls back to a transcript snippet if no summary."""
    try:
        conn = _db()
        try:
            rows = conn.execute(
                "SELECT ts, summary, transcript FROM conversations ORDER BY id DESC LIMIT ?",
                (k,)).fetchall()
        finally:
            conn.close()
    except Exception as e:
        _log(f"memory.recent failed: {e!r}")
        return ""
    out = []
    for ts, summary, transcript in rows:
        body = (summary or "").strip() or (transcript or "")[:200].strip()
        if body:
            out.append(f"[{ts}] {body}")
    convos = "\n".join(out)
    learned = top_learnings(8)
    parts = []
    if learned:
        parts.append("What I've learned about you:\n" + learned)
    if convos:
        parts.append("Recent conversations:\n" + convos)
    return "\n\n".join(parts)


def _fts_terms(query: str) -> str:
    """Turn arbitrary text into a safe FTS5 MATCH expression (OR of word tokens)."""
    tokens = re.findall(r"\w+", query or "")
    return " OR ".join(tokens)


def _recall_own(query: str, k: int) -> str:
    match = _fts_terms(query)
    if not match:
        return ""
    try:
        conn = _db()
        try:
            rows = conn.execute(
                "SELECT c.ts, c.summary, c.transcript FROM conversations_fts f "
                "JOIN conversations c ON c.id = f.rowid "
                "WHERE conversations_fts MATCH ? ORDER BY rank LIMIT ?",
                (match, k)).fetchall()
        finally:
            conn.close()
    except Exception as e:
        _log(f"memory._recall_own failed: {e!r}")
        return ""
    out = []
    for ts, summary, transcript in rows:
        body = (summary or "").strip() or (transcript or "")[:200].strip()
        if body:
            out.append(f"[{ts}] {body}")
    return "\n".join(out)


# --- durable learnings (the continuous-learning layer) ----------------------
def _norm(text: str) -> str:
    """Normalize for duplicate detection: lowercase, collapse whitespace."""
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def _active_learnings(limit: int = 200) -> list:
    """Active learnings as (id, type, text, score) sorted by strength*recency desc.
    Score is computed in Python (small set), so SQL stays trivial."""
    try:
        conn = _db()
        try:
            rows = conn.execute(
                "SELECT id, type, text, strength, ts_epoch, last_used_epoch "
                "FROM learnings WHERE status='active'").fetchall()
        finally:
            conn.close()
    except Exception as e:
        _log(f"memory._active_learnings failed: {e!r}")
        return []
    now = time.time()
    scored = []
    for lid, typ, text, strength, ts_epoch, last_used in rows:
        age_days = max(0.0, (now - (last_used or ts_epoch or now)) / 86400.0)
        score = (strength or 1.0) / (1.0 + age_days)   # decay: unused old ones sink
        scored.append((lid, typ, text, score))
    scored.sort(key=lambda r: r[3], reverse=True)
    return scored[:limit]


def top_learnings(k: int = 8) -> str:
    """Top-k active learnings as a compact block for the live prompt / recall."""
    rows = _active_learnings(k)
    if not rows:
        return ""
    return "\n".join(f"- ({typ}) {text}" for _id, typ, text, _s in rows)


def learnings_block(k: int = 30) -> str:
    """Active learnings WITH ids — for the extractor so it can flag supersedes."""
    rows = _active_learnings(k)
    return "\n".join(f"{lid}: [{typ}] {text}" for lid, typ, text, _s in rows)


def panel_stats(k: int = 4) -> dict:
    """Counts + a few recent learnings for the Settings memory panel. Display-only:
    any failure returns zeros/empty so Settings never breaks on a memory hiccup."""
    conv = learn = 0
    try:
        conn = _db()
        try:
            conv = conn.execute("SELECT COUNT(*) FROM conversations").fetchone()[0]
            learn = conn.execute(
                "SELECT COUNT(*) FROM learnings WHERE status='active'").fetchone()[0]
        finally:
            conn.close()
    except Exception as e:
        _log(f"memory.panel_stats failed: {e!r}")
    recent = [text for _id, _typ, text, _s in _active_learnings(k)]
    return {"conversations": conv, "learnings": learn, "recent": recent}


def reinforce(learning_id: int) -> None:
    """A restated/confirmed learning: bump strength + uses, refresh recency."""
    if not learning_id:
        return
    try:
        conn = _db()
        try:
            conn.execute(
                "UPDATE learnings SET strength = strength + 0.5, uses = uses + 1, "
                "last_used_epoch = ? WHERE id = ?", (int(time.time()), learning_id))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        _log(f"memory.reinforce failed: {e!r}")


def supersede(old_id: int, new_id) -> None:
    """Mark a learning superseded (self-correction) — kept for history, dropped from recall."""
    if not old_id:
        return
    try:
        conn = _db()
        try:
            conn.execute(
                "UPDATE learnings SET status='superseded', superseded_by=? WHERE id=?",
                (new_id, old_id))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        _log(f"memory.supersede failed: {e!r}")


def add_learning(type_: str, text: str, source_conv_id=None) -> int:
    """Insert a durable learning; returns its id. If an active learning of the same
    type already has the same normalized text, reinforce that one instead of
    duplicating (returns its id). New active learnings are mirrored to claude-mem.

    ponytail: dedup is exact normalized-text match; semantic dedup is the model's job
    (the extractor is given existing learnings). Upgrade path: FTS near-dup match here."""
    text = (text or "").strip()
    if not text:
        return 0
    type_ = (type_ or "fact").strip().lower()
    if type_ not in ("preference", "fact", "correction"):
        type_ = "fact"
    try:
        conn = _db()
        try:
            norm = _norm(text)
            for lid, ltext in conn.execute(
                    "SELECT id, text FROM learnings WHERE status='active' AND type=?",
                    (type_,)).fetchall():
                if _norm(ltext) == norm:
                    conn.close()
                    reinforce(lid)
                    return lid
            cur = conn.execute(
                "INSERT INTO learnings(created_at, ts_epoch, type, text, source_conv_id, "
                "last_used_epoch) VALUES (?,?,?,?,?,?)",
                (time.strftime("%Y-%m-%d %H:%M"), int(time.time()), type_, text,
                 source_conv_id, int(time.time())))
            lid = cur.lastrowid
            conn.execute("INSERT INTO learnings_fts(rowid, text) VALUES (?,?)", (lid, text))
            conn.commit()
        finally:
            try:
                conn.close()
            except Exception:
                pass
    except Exception as e:
        _log(f"memory.add_learning failed: {e!r}")
        return 0
    _mirror_claude_mem(text)
    return lid


def extract_apply(payload: dict, conv_id=None) -> None:
    """Apply one extraction result: {summary, new_learnings:[{type,text}], supersede:[ids]}.
    Inserts/reinforces new learnings, marks superseded ids. Summary is handled by the
    caller via set_summary. Best-effort; bad shapes are skipped."""
    if not isinstance(payload, dict):
        return
    for item in (payload.get("new_learnings") or []):
        try:
            add_learning(item.get("type", "fact"), item.get("text", ""), conv_id)
        except Exception as e:
            _log(f"extract_apply add failed: {e!r}")
    for old_id in (payload.get("supersede") or []):
        try:
            supersede(int(old_id), None)
        except Exception as e:
            _log(f"extract_apply supersede failed: {e!r}")


def _mirror_claude_mem(text: str) -> None:
    """Best-effort mirror of one learning into claude-mem's observations store, so it
    surfaces in the wider memory too. Isolated + non-fatal: any failure (schema change,
    bad path, off) never blocks the app-owned write. Gated by config.

    Note: claude-mem's created_at_epoch is in MILLISECONDS (unlike our seconds)."""
    if not config.get("live.memory.mirror_claude_mem", False):
        return
    db = os.path.expanduser(config.get("live.memory.claude_mem_db",
                                       "~/.claude-mem/claude-mem.db"))
    if not os.path.exists(db):
        return
    sid, project = "thrivbe-voice", "Thrivbe Voice"
    try:
        conn = sqlite3.connect(db, timeout=5)
        try:
            now_ms = int(time.time() * 1000)
            iso = time.strftime("%Y-%m-%dT%H:%M:%S")
            # Ensure our one dedicated session row exists (content/memory ids are UNIQUE).
            conn.execute(
                "INSERT OR IGNORE INTO sdk_sessions(content_session_id, memory_session_id, "
                "project, started_at, started_at_epoch, status) VALUES (?,?,?,?,?,'active')",
                (sid, sid, project, iso, now_ms))
            # observations_ai trigger keeps claude-mem's FTS in sync automatically.
            conn.execute(
                "INSERT INTO observations(memory_session_id, project, text, type, title, "
                "created_at, created_at_epoch) VALUES (?,?,?,'discovery',?,?,?)",
                (sid, project, text, text[:80], iso, now_ms))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        _log(f"memory._mirror_claude_mem failed: {e!r}")


# --- external recall providers (read-only, swappable) -----------------------
def _recall_claude_mem(query: str, k: int) -> str:
    """Read-only query of claude-mem's observations FTS index. Isolated here so a
    claude-mem version change touches nothing else; any failure returns ''."""
    match = _fts_terms(query)
    if not match:
        return ""
    db = os.path.expanduser(config.get("live.memory.claude_mem_db",
                                       "~/.claude-mem/claude-mem.db"))
    if not os.path.exists(db):
        return ""
    try:
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            rows = conn.execute(
                "SELECT o.title, o.subtitle FROM observations_fts f "
                "JOIN observations o ON o.id = f.rowid "
                "WHERE observations_fts MATCH ? ORDER BY rank LIMIT ?",
                (match, k)).fetchall()
        finally:
            conn.close()
    except Exception as e:
        _log(f"memory._recall_claude_mem failed: {e!r}")
        return ""
    out = []
    for title, subtitle in rows:
        line = (title or "").strip()
        if subtitle:
            line += f" — {subtitle.strip()}"
        if line:
            out.append(f"- {line}")
    return "\n".join(out)


def _recall_command(query: str, k: int) -> str:
    """Run the user's own knowledge-base command. `{query}` is substituted in.
    This is the 'connect to whatever you already have' hook for the sold product."""
    cmd = config.get("live.memory.command")
    if not cmd:
        return ""
    # ponytail: shlex.quote below neutralizes {query} injection. The `cmd` TEMPLATE is
    # still trusted config — a model with agentic-shell access could rewrite it in
    # config.json. Closing that needs a command allowlist (product decision); not done here.
    try:
        full = cmd.replace("{query}", shlex.quote(query))   # quote: query is a single arg, not shell
        r = subprocess.run(["sh", "-c", full], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=20)
        return (r.stdout or "").strip()[:2000]
    except Exception as e:
        _log(f"memory._recall_command failed: {e!r}")
        return ""


def recall(query: str, k: int = 5) -> str:
    """Search the app-owned store, then append the configured provider's hits.
    Returns a compact, labeled block (or '' if nothing relevant)."""
    parts = []
    learned = top_learnings(8)
    if learned:
        parts.append("What I've learned about you:\n" + learned)
    own = _recall_own(query, k)
    if own:
        parts.append("From your past conversations:\n" + own)

    provider = config.get("live.memory.provider", "none")
    ext = ""
    if provider == "claude-mem":
        ext = _recall_claude_mem(query, k)
    elif provider == "command":
        ext = _recall_command(query, k)
    if ext:
        parts.append(f"From your wider memory ({provider}):\n" + ext)

    return "\n\n".join(parts)


def _log(msg: str) -> None:
    try:
        config.activity(f"🧠 {msg}")
    except Exception:
        pass


# --- self-check + CLI -------------------------------------------------------
def demo() -> None:
    """Runnable check: record → recent → recall round-trips on a throwaway DB."""
    global DB_PATH
    import tempfile
    saved = DB_PATH
    DB_PATH = os.path.join(tempfile.mkdtemp(), "demo.db")
    try:
        cid = record("user: where is the front skill?\nagent: it's in skills/front",
                     summary="Discussed locating the front email skill.")
        assert cid, "record returned no id"
        assert "front" in recent(5).lower(), "recent() missing the row"
        assert "front" in _recall_own("front skill", 5).lower(), "FTS recall missed it"
        set_summary(cid, "Updated: front skill lives at skills/front.")
        assert "lives at" in _recall_own("front", 5).lower(), "set_summary didn't reindex"

        # learnings: add → reinforce (no dup) → supersede
        lid = add_learning("preference", "Robin prefers terse answers.")
        assert lid, "add_learning returned no id"
        assert "terse" in top_learnings(8).lower(), "top_learnings missing the learning"
        same = add_learning("preference", "robin   prefers TERSE answers.")  # normalized dup
        assert same == lid, "duplicate learning was not folded into the original"
        conn = _db()
        try:
            strength, uses = conn.execute(
                "SELECT strength, uses FROM learnings WHERE id=?", (lid,)).fetchone()
        finally:
            conn.close()
        assert strength > 1.0 and uses >= 1, "reinforce didn't bump strength/uses"
        extract_apply({"new_learnings": [{"type": "preference",
                       "text": "Robin prefers detailed answers."}],
                       "supersede": [lid]}, conv_id=cid)
        block = top_learnings(8).lower()
        assert "detailed" in block, "new learning not active"
        assert "terse" not in block, "superseded learning still in recall"
        print("memory.py demo OK")
    finally:
        DB_PATH = saved


if __name__ == "__main__":
    import sys
    args = sys.argv[1:]
    if not args:
        print("usage: memory.py [recall <query> | recent | learnings | demo]")
    elif args[0] == "demo":
        demo()
    elif args[0] == "learnings":
        print(top_learnings(int(args[1]) if len(args) > 1 else 8)
              or "(nothing learned yet)")
    elif args[0] == "recent":
        print(recent(int(args[1]) if len(args) > 1 else 5))
    elif args[0] == "recall":
        print(recall(" ".join(args[1:])) or "(nothing relevant in memory)")
    else:
        print(f"unknown command: {args[0]}")
