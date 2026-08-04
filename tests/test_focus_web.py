"""focus + web tools: registration, gating, and the contracts live_session relies on."""
import os
import shutil

import config
import focus
import web
from tools import TOOLS

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
    from tools import to_gemini_schema
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


def test_prompt_carries_focus_into_a_new_session(tmp_app, tmp_path):
    import live_prompt
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


def test_survives_a_login_launched_app(monkeypatch):
    """A .app opened from Finder/at login inherits launchd's minimal PATH. That broke
    the call twice — the bare binary didn't resolve, and firecrawl's `env node` shebang
    couldn't find node. Both must survive without falling back to a shell."""
    monkeypatch.setenv("PATH", "/usr/bin:/bin:/usr/sbin:/sbin")
    assert os.path.isabs(web.firecrawl_bin()), "binary must resolve without PATH"
    assert os.access(web.firecrawl_bin(), os.X_OK)
    path = web._env()["PATH"].split(os.pathsep)
    node = shutil.which("node", path=web._env()["PATH"])
    assert node, "subprocess PATH must still be able to resolve the node interpreter"
    assert any(d in path for d in web.TOOL_DIRS)
