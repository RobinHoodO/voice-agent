"""focus + web tools: registration, gating, and the contracts live_session relies on."""
import os
import shutil

from core import config
from core import focus
from core import web
from core.tools import TOOLS

NEW_TOOLS = ("focus", "web_search", "read_url")


def test_registered_and_unique():
    names = [t["name"] for t in TOOLS]
    assert len(names) == len(set(names))
    for n in NEW_TOOLS:
        assert n in names


def test_available_when_agentic_shell_is_off():
    """live_session drops run_shell/delegate when the shell is off — the context and
    web tools are read-only and must survive that filter."""
    kept = [t["name"] for t in TOOLS if t.get("name") not in ("run_shell", "delegate")]
    for n in NEW_TOOLS:
        assert n in kept


def test_gemini_schema_accepts_new_tools():
    from core.tools import to_gemini_schema
    decls = to_gemini_schema(TOOLS)[0]["functionDeclarations"]
    assert {d["name"] for d in decls} >= set(NEW_TOOLS)


# --- focus -----------------------------------------------------------------
def _workspace(tmp_path):
    acme = tmp_path / "clients" / "Acme - Jane Doe"
    (acme / "Meetings").mkdir(parents=True)
    (acme / "README.md").write_text("Acme is a paying client.")
    (tmp_path / "projects" / "other-thing").mkdir(parents=True)
    return str(tmp_path), str(acme)


def test_focus_resolves_spoken_phrasing(tmp_path):
    ws, acme = _workspace(tmp_path)
    assert focus.resolve("the Acme client", ws)[0][2] == acme
    assert focus.resolve("Acme", ws)[0][2] == acme
    assert focus.resolve("something that does not exist", ws) == []


def test_focus_saves_and_clears(tmp_app, tmp_path, monkeypatch):
    ws, acme = _workspace(tmp_path)
    monkeypatch.setattr(config, "get",
                        lambda p, d=None: ws if p == "live.workspace" else _real_get(p, d))
    out = focus.focus({"subject": "Acme"})
    assert "Now focused on Acme - Jane Doe" in out
    assert "Acme is a paying client." in out          # docs actually read in
    assert config.load()["live"]["focus"]["dir"] == acme
    assert focus.current()["dir"] == acme

    assert "cleared" in focus.focus({"subject": "clear"}).lower()
    assert config.load()["live"]["focus"] is None
    assert focus.current() == {}


_real_get = config.get


def test_focus_miss_points_at_the_search_tools(tmp_app, tmp_path, monkeypatch):
    ws, _ = _workspace(tmp_path)
    monkeypatch.setattr(config, "get",
                        lambda p, d=None: ws if p == "live.workspace" else _real_get(p, d))
    out = focus.focus({"subject": "zzz nothing"})
    assert "semsearch_query" in out and "Nothing under" in out


def test_focus_digest_respects_budget(tmp_path):
    _ws, acme = _workspace(tmp_path)
    (tmp_path / "clients" / "Acme - Jane Doe" / "huge.md").write_text("y" * 60000)
    assert len(focus.digest(acme)) < focus.DOC_BUDGET + 2000


# --- focus output is prompt too --------------------------------------------
# The digest is the TOOL RESULT, and a tool result lands AFTER the system prompt — the
# strongest instruction position there is. `focus` is in the phone's tool list, so every
# focus call on that surface used to answer with "Your shell is now IN this folder",
# contradicting the surface note a few thousand characters above it.

def _mixed_folder(tmp_path):
    """One non-text file (drives the inventory line) and one document (drives the
    loaded/truncated line) — enough to reach every branch that used to name a shell."""
    _ws, acme = _workspace(tmp_path)
    (tmp_path / "clients" / "Acme - Jane Doe" / "deck.pdf").write_bytes(b"%PDF-1.4" + b"0" * 2000)
    return acme


def test_focus_output_never_names_a_shell_on_a_shell_less_surface(tmp_path, monkeypatch):
    acme = _mixed_folder(tmp_path)
    monkeypatch.setattr(focus, "PER_DOC", 8)      # force the TRUNCATED branch as well
    out = focus.digest(acme, profile="phone")
    assert "run_shell" not in out
    assert "pdftotext" not in out                  # the shell recipe goes with it
    assert "Your shell is now IN this folder" not in out
    assert "TRUNCATED" in out, "the truncation branch has to actually be exercised here"
    assert "deck.pdf" in out, "the inventory is still worth having without a shell"
    assert "os_delegate" in out                    # ...and it says who reads it instead


def test_focus_output_on_an_empty_folder_is_also_shell_free(tmp_path):
    bare = tmp_path / "clients" / "Bare"
    bare.mkdir(parents=True)
    out = focus.digest(str(bare), profile="phone")
    assert "Nothing readable as text" in out and "run_shell" not in out


def test_focus_output_defaults_to_the_strict_surface(tmp_path):
    """A caller that forgets to thread the profile through must under-promise, never
    over-promise — the same fail-closed direction as capabilities.tools_for."""
    assert "run_shell" not in focus.digest(_mixed_folder(tmp_path))


def test_focus_output_at_the_desk_is_unchanged(tmp_path):
    """Nothing was taken away from the surface Robin sits at."""
    out = focus.digest(_mixed_folder(tmp_path), profile="mac")
    assert "Your shell is now IN this folder" in out
    assert "pdftotext" in out


def test_the_focus_tool_hands_its_session_profile_to_the_digest(tmp_app, tmp_path, monkeypatch):
    """The plumbing, not just the function: live_session calls focus(args, profile=...),
    and focus has to pass it on or the whole surface-awareness stops at the door."""
    ws, _acme = _workspace(tmp_path)
    (tmp_path / "clients" / "Acme - Jane Doe" / "deck.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setattr(config, "get",
                        lambda p, d=None: ws if p == "live.workspace" else _real_get(p, d))
    assert "run_shell" not in focus.focus({"subject": "Acme"}, profile="phone")
    assert "pdftotext" in focus.focus({"subject": "Acme"}, profile="mac")


def test_prompt_carries_focus_into_a_new_session(tmp_app, tmp_path):
    from core import live_prompt
    cfg = {"live": {"focus": {"subject": "acme", "name": "Acme", "dir": str(tmp_path)}}}
    block = live_prompt._build_live_instructions("", cfg)
    assert "Current focus: Acme" in block
    assert str(tmp_path) in block


# --- web -------------------------------------------------------------------
def test_web_results_are_labelled_untrusted(monkeypatch):
    monkeypatch.setattr(web, "_run", lambda argv, timeout: ("Title\n  URL: http://x", None))
    for out in (web.web_search({"query": "q"}), web.read_url({"url": "https://x.com"})):
        assert web.UNTRUSTED in out


def test_web_search_builds_a_safe_argv(monkeypatch):
    seen = {}

    def fake(argv, timeout):
        seen["argv"] = argv
        return ("ok", None)

    monkeypatch.setattr(web, "_run", fake)
    web.web_search({"query": "rm -rf / ; echo $OPENAI_API_KEY", "recency": "week"})
    # the model's text stays one argv element — it can never become shell syntax
    assert seen["argv"][2] == "rm -rf / ; echo $OPENAI_API_KEY"
    assert "qdr:w" in seen["argv"]


def test_web_rejects_bad_input(monkeypatch):
    monkeypatch.setattr(web, "_run", lambda argv, timeout: ("ok", None))
    assert web.web_search({"query": "  "}).startswith("I need")
    assert web.read_url({"url": "file:///etc/passwd"}).startswith("I need a full http")


def test_web_errors_degrade_to_a_sentence(monkeypatch):
    monkeypatch.setattr(web, "_run", lambda argv, timeout: (None, "The web request failed."))
    assert web.web_search({"query": "q"}) == "The web request failed."


def test_web_search_env_has_no_secrets():
    """_run hands firecrawl a scrubbed env; the CLI reads its own key from disk."""
    assert not {k for k in web._env() if k.endswith("_API_KEY")}


def test_survives_a_login_launched_app(monkeypatch, tmp_path):
    """A .app opened from Finder/at login inherits launchd's minimal PATH. That broke
    the call twice — the bare binary didn't resolve, and firecrawl's `env node` shebang
    couldn't find node. Both must survive without falling back to a shell.

    The tools here are stubs in a tmp dir, deliberately. This test used to assert
    against whatever was actually installed on the machine, which meant it passed on
    Robin's Mac and failed on any clean one — the first hosted CI run died right here.
    A test that only holds on one laptop is not a regression test. What is being
    checked is the resolution logic in `firecrawl_bin()` and the PATH widening in
    `_env()`; neither needs a real firecrawl or a real node to be exercised.
    """
    tools = tmp_path / "bin"
    tools.mkdir()
    # The firecrawl stub carries the SAME shebang as the real CLI. That is the whole
    # point: `#!/bin/sh` would resolve via the kernel and prove nothing, because the
    # second half of the original bug was `env` failing to find node on a minimal PATH.
    firecrawl = tools / "firecrawl"
    firecrawl.write_text("#!/usr/bin/env node\n")
    node = tools / "node"
    node.write_text("#!/bin/sh\nexit 0\n")
    for stub in (firecrawl, node):
        stub.chmod(0o755)
    monkeypatch.setattr(web, "TOOL_DIRS", (str(tools),))
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")

    # Not on PATH, so this can only come from the TOOL_DIRS fallback.
    assert web.firecrawl_bin() == str(tools / "firecrawl"), "binary must resolve without PATH"
    assert os.access(web.firecrawl_bin(), os.X_OK)
    path = web._env()["PATH"].split(os.pathsep)
    assert any(d in path for d in web.TOOL_DIRS)
    # Specifically OUR node, not one that happens to be installed on the machine —
    # otherwise this passes for the wrong reason on a developer laptop.
    assert shutil.which("node", path=web._env()["PATH"]) == str(node)

    # And the part no assertion above can reach: actually spawn it. `_run` passes
    # `env=_env()`, so `/usr/bin/env node` inside the shebang is resolved against the
    # widened PATH at execve time. If that widening ever regresses this raises
    # FileNotFoundError and `_run` returns the "isn't installed" sentence instead.
    out, err = web._run([web.firecrawl_bin(), "search", "q"], timeout=10)
    assert err is None, f"the env-node shebang did not resolve: {err}"
    assert out is not None


def test_firecrawl_bin_falls_back_to_the_bare_name_when_absent(monkeypatch, tmp_path):
    """Genuinely not installed → return the bare name so `_run` reports it missing.

    This is the branch the clean CI runner was actually hitting, and nothing covered
    it. `_run` turns the resulting FileNotFoundError into "The firecrawl CLI isn't
    installed on this Mac", which is the correct behaviour — guessing a path would be
    worse than saying so.
    """
    monkeypatch.setattr(web, "TOOL_DIRS", (str(tmp_path / "nothing-here"),))
    monkeypatch.setenv("PATH", str(tmp_path / "also-empty"))

    assert web.firecrawl_bin() == "firecrawl"
    assert web._run([web.firecrawl_bin(), "search", "q"], timeout=5) == (
        None, "The firecrawl CLI isn't installed on this Mac, so I can't search the web.")
