#!/usr/bin/env python3
"""Conversation focus — point Pam at one client/project folder and keep it as context.

`focus("Mingle")` resolves a SPOKEN subject to a real folder under the workspace
(clients/, projects/, lab/), reads that folder's key docs, and returns a digest.

The digest is returned as the tool RESULT, which the Realtime API keeps in conversation
history — so it stays in context for the rest of the session with no separate injection
machinery. The resolved folder is also saved to config (`live.focus`) so a NEW session
(idle timeout, or an auto-wake announcement) still knows what Robin is working on;
live_prompt reads it back at startup.

ponytail: top-level markdown only, no recursion — a client folder's own docs are what
"talk about this client" means, and a recursive walk of projects/ would stall the turn.
Pam can run_shell deeper once focused.

Self-check:  python3 focus.py
"""
import difflib
import os

import config

SEARCH_ROOTS = ("clients", "projects", "lab")
PRIORITY_DOCS = ("CLAUDE.md", "README.md")

# Load the whole folder, not a teaser. Gemini Live carries a very large context, and a
# truncated doc is worse than useless here: Pam half-remembers it and answers from a
# fragment instead of the real thing (which is exactly what happened with the Biomattera
# tech-concept PRD). Budgets are a runaway guard, not a diet — override live.focus_budget.
DOC_BUDGET = 150_000       # ~40k tokens; a whole client folder fits well inside this
PER_DOC = 60_000           # a 43KB meeting transcript must land WHOLE, not clipped
MAX_DOCS = 40
MAX_DEPTH = 3              # deep enough for Meetings/, Leads/, docs/ — not a source tree
READ_EXT = (".md", ".mdx", ".txt", ".rst")
# Directories that are never conversation context, only bulk. Skipped when walking.
SKIP_DIRS = {"node_modules", ".git", "dist", "build", ".next", "__pycache__", ".venv",
             "venv", ".tmp", "target", "vendor", ".cache", "coverage", ".pytest_cache"}
MATCH_FLOOR = 0.6          # below this, a folder name isn't really what he said
CLEAR_WORDS = {"", "none", "clear", "nothing", "off", "no folder", "unfocus", "reset"}

# Spoken filler that would otherwise drag every score toward the mean.
_STOP = {"the", "a", "an", "my", "our", "client", "clients", "project", "projects",
         "folder", "stuff", "work", "on", "about", "for", "with", "please", "lets",
         "talk", "context", "focus"}


def _log(msg: str) -> None:
    try:
        from agent import LOG
        LOG(f"focus: {msg}")
    except Exception:
        pass


def _norm(text: str) -> str:
    """Lowercase alnum words, filler dropped — 'the Mingle client' -> 'mingle'."""
    cleaned = "".join(ch.lower() if (ch.isalnum() or ch.isspace()) else " " for ch in text or "")
    return " ".join(w for w in cleaned.split() if w not in _STOP)


def _score(subject: str, name: str) -> float:
    s, n = _norm(subject), _norm(name)
    if not s or not n:
        return 0.0
    if s == n:
        return 1.0
    if s in n or n in s:
        # Containment is a strong signal ("biomattera" in "biomattera clarissa
        # magalhaes"), but prefer the tighter match when several folders contain it.
        return 0.75 + 0.25 * (min(len(s), len(n)) / max(len(s), len(n)))
    return difflib.SequenceMatcher(None, s, n).ratio()


def candidates(ws: str) -> list:
    """(display name, absolute path) for every focusable folder under the workspace."""
    found = []
    for root in SEARCH_ROOTS:
        base = os.path.join(ws, root)
        try:
            names = sorted(os.listdir(base))
        except OSError:
            continue
        for name in names:
            path = os.path.join(base, name)
            if name.startswith(".") or not os.path.isdir(path):
                continue
            found.append((name, path))
    return found


def resolve(subject: str, ws: str) -> list:
    """Best-first [(score, name, path)] above the match floor."""
    scored = [(_score(subject, name), name, path) for name, path in candidates(ws)]
    scored = [row for row in scored if row[0] >= MATCH_FLOOR]
    scored.sort(key=lambda row: (-row[0], row[1]))
    return scored


def _read(path: str, limit: int) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return fh.read(limit).strip()
    except OSError as e:
        _log(f"unreadable {path}: {e!r}")
        return ""


def walk(folder: str) -> tuple:
    """(readable docs, everything else) as absolute paths, recursively but shallowly.

    'Everything else' still gets LISTED in the digest — a PDF or spreadsheet Pam can't
    read inline is exactly the thing she should know exists and offer to open."""
    docs, others = [], []
    folder = os.path.abspath(folder)
    base_depth = folder.rstrip(os.sep).count(os.sep)
    for root, dirs, files in os.walk(folder):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
        if root.rstrip(os.sep).count(os.sep) - base_depth >= MAX_DEPTH:
            dirs[:] = []
        for name in sorted(files):
            if name.startswith("."):
                continue
            path = os.path.join(root, name)
            (docs if name.lower().endswith(READ_EXT) else others).append(path)
    return docs, others


def _doc_paths(folder: str) -> list:
    """Priority docs first, then the rest newest-first, so the budget is spent on the
    orientation docs and the freshest thinking before older material."""
    docs, _others = walk(folder)
    picked, seen = [], set()
    for name in PRIORITY_DOCS:
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            picked.append(path)
            seen.add(path)
    dated = []
    for path in docs:
        if path in seen:
            continue
        try:
            dated.append((os.path.getmtime(path), path))
        except OSError:
            pass
    dated.sort(reverse=True)
    picked.extend(path for _, path in dated[:max(0, MAX_DOCS - len(picked))])
    return picked


def _rel(path: str, folder: str) -> str:
    return os.path.relpath(path, folder)


def digest(folder: str) -> str:
    """The folder's full readable content, plus an inventory of what else is in it.

    The shell is cd'd here when focus runs, so every path below is usable as-is."""
    budget = config.get("live.focus_budget") or DOC_BUDGET
    try:
        budget = max(10_000, int(budget))
    except (TypeError, ValueError):
        budget = DOC_BUDGET
    docs, others = walk(folder)
    lines = [f"Focused folder: {folder}",
             "Your shell is now IN this folder, so relative paths work: `cat proposal.md`."]
    if others:
        inventory = []
        for path in others[:40]:
            try:
                kb = max(1, os.path.getsize(path) // 1024)
                inventory.append(f"{_rel(path, folder)} ({kb}KB)")
            except OSError:
                inventory.append(_rel(path, folder))
        lines.append("Other files here — not text, so not loaded below, but you can open "
                     "them with run_shell if asked (PDFs: `pdftotext <file> -`):\n  "
                     + "\n  ".join(inventory))
    used, loaded, clipped = 0, [], []
    for path in _doc_paths(folder):
        if used >= budget:
            break
        body = _read(path, min(PER_DOC, budget - used))
        if not body:
            continue
        used += len(body)
        rel = _rel(path, folder)
        try:
            whole = os.path.getsize(path) <= len(body.encode("utf-8", "replace")) + 2
        except OSError:
            whole = True
        (loaded if whole else clipped).append(rel)
        lines.append(f"\n--- {rel} ---\n{body}")
    if not used:
        lines.append("(Nothing readable as text here — use run_shell to look deeper.)")
    else:
        note = f"\nLoaded {len(loaded) + len(clipped)} document(s) in full ({used:,} chars)."
        if clipped:
            note = (f"\nLoaded {len(loaded) + len(clipped)} document(s), {used:,} chars. "
                    f"TRUNCATED (read the rest with run_shell before answering on them): "
                    + ", ".join(clipped))
        lines.append(note)
    return "\n".join(lines)


def current() -> dict:
    """The saved focus, or {} — used by live_prompt at session start."""
    foc = config.get("live.focus")
    return foc if isinstance(foc, dict) and foc.get("dir") else {}


def focus(args: dict) -> str:
    subject = (args.get("subject") or "").strip()
    ws = os.path.expanduser(config.get("live.workspace") or "~")
    if subject.lower() in CLEAR_WORDS:
        config.set_("live.focus", None)
        _log("cleared")
        return "Focus cleared — back to the whole workspace."
    matches = resolve(subject, ws)
    if not matches:
        return (f"Nothing under {ws} matches {subject!r}. "
                "Try semsearch_query or hybrid_rag_search to find it in the knowledge "
                "base instead, or say a folder name.")
    _score_, name, path = matches[0]
    config.set_("live.focus", {"subject": subject, "name": name, "dir": path})
    _log(f"{subject!r} -> {path}")
    config.activity(f"🎯  focused on {name}")
    head = f"Now focused on {name}."
    others = ", ".join(n for _, n, _ in matches[1:4])
    if others:
        head += f" (Also matched: {others} — say a name to switch.)"
    return head + "\n\n" + digest(path)


if __name__ == "__main__":
    import shutil
    import tempfile

    # --- scoring: spoken phrasing must land on the real folder
    assert _norm("the Mingle client") == "mingle"
    assert _score("Mingle", "Mingle") == 1.0
    assert _score("biomattera", "Biomattera - Clarissa Magalhaes") > MATCH_FLOOR
    assert _score("the Mingle client", "Mingle") == 1.0
    assert _score("mingle", "bloom") < MATCH_FLOOR
    # a tighter containment outranks a looser one
    assert _score("bloom", "bloom") > _score("bloom", "bloom-experiments-archive")

    tmp = tempfile.mkdtemp()
    try:
        acme = os.path.join(tmp, "clients", "Acme - Jane Doe")
        os.makedirs(os.path.join(acme, "Meetings"))
        os.makedirs(os.path.join(tmp, "projects", "unrelated-thing"))
        with open(os.path.join(acme, "README.md"), "w") as f:
            f.write("Acme is a paying client.")
        with open(os.path.join(acme, "notes-2026.md"), "w") as f:
            f.write("Latest call went well.")

        hits = resolve("Acme", tmp)
        assert hits and hits[0][1] == "Acme - Jane Doe", hits
        assert not resolve("zzz-nonexistent", tmp)

        # nested docs and non-text files both have to surface
        with open(os.path.join(acme, "Meetings", "2026-08-03-call.md"), "w") as f:
            f.write("Clarissa wants a coordination agent.")
        with open(os.path.join(acme, "deck.pdf"), "wb") as f:
            f.write(b"%PDF-1.4" + b"0" * 3000)

        d = digest(acme)
        assert "Acme is a paying client." in d          # README pulled in
        assert "notes-2026.md" in d                     # top-level markdown pulled in
        assert "coordination agent" in d, "nested docs must be read, not just listed"
        assert "deck.pdf" in d, "non-text files must still be inventoried"
        assert "pdftotext" in d                         # and she's told how to open them

        # a big doc lands WHOLE — the old 1400-char clip is what made her half-remember
        with open(os.path.join(acme, "transcript.md"), "w") as f:
            f.write("A" * 40000 + "ENDMARKER")
        assert "ENDMARKER" in digest(acme), "a 40KB transcript must not be truncated"

        # ...but the total is still bounded, so a runaway folder can't blow up the turn
        for i in range(12):
            with open(os.path.join(acme, f"bulk{i}.md"), "w") as f:
                f.write("z" * 30000)
        assert len(digest(acme)) < DOC_BUDGET + 20000

        # skip-dirs stay out
        os.makedirs(os.path.join(acme, "node_modules", "pkg"))
        with open(os.path.join(acme, "node_modules", "pkg", "README.md"), "w") as f:
            f.write("NOISE_FROM_NODE_MODULES")
        assert "NOISE_FROM_NODE_MODULES" not in digest(acme)

        # an empty folder still returns something usable, not a crash
        bare = os.path.join(tmp, "clients", "Bare")
        os.makedirs(bare)
        assert "Nothing readable as text" in digest(bare)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    print("FOCUS OK")
