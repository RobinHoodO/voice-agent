"""Offline coverage for Pam's herdr inventory, reuse, and completion handoff."""
import asyncio
import os
import queue
import time

import agent
import config
import live_session
import live_prompt
import tools


class FakeHerdr:
    def __init__(self):
        self.calls = []
        self.state = {"agents": [], "panes": [], "workspaces": []}

    def __call__(self, *args, timeout=10):
        self.calls.append(args)
        if args[:2] == ("workspace", "list"):
            return {"workspaces": self.state["workspaces"]}
        if args[:2] == ("agent", "list"):
            return {"agents": self.state["agents"]}
        if args[:2] == ("pane", "list"):
            return {"panes": self.state["panes"]}
        if args[:2] == ("pane", "process-info"):
            pane = next(p for p in self.state["panes"] if p["pane_id"] == args[2])
            return {"argv": pane.get("argv", [pane.get("agent", "zsh")])}
        if args[:2] == ("agent", "rename"):
            pane_id, name = args[2], args[3]
            agent_rec = next((a for a in self.state["agents"] if a["pane_id"] == pane_id), None)
            if agent_rec is None:
                self.state["agents"].append({"name": name, "pane_id": pane_id,
                                             "agent_status": "idle"})
            else:
                agent_rec["name"] = name
            return {}
        if args[:2] == ("agent", "start"):
            pane_id = f"w9:p{len(self.state['panes']) + 1}"
            rec = {"name": args[2], "pane_id": pane_id, "agent_status": "working"}
            self.state["agents"].append(rec)
            self.state["panes"].append({"pane_id": pane_id, "workspace_id": "w9", "agent": "claude"})
            return {"agent": rec}
        return {}


def _install_fake(monkeypatch, tmp_app):
    fake = FakeHerdr()
    monkeypatch.setattr(tools, "_herdr", fake)
    monkeypatch.setattr(tools, "_herdr_up", lambda: True)
    monkeypatch.setattr(tools.time, "sleep", lambda _seconds: None)
    return fake


def _watched_cfg():
    return {"live": {"delegate": "claude", "show_task_terminals": True, "workspace": "/tmp"}}


def test_fleet_joins_workspace_agents_and_bare_shell_labels(monkeypatch, tmp_app):
    fake = _install_fake(monkeypatch, tmp_app)
    fake.state.update({
        "workspaces": [{"workspace_id": "w1", "label": "home"},
                       {"workspace_id": "w9", "label": "voice"}],
        "panes": [{"pane_id": "w1:p2", "workspace_id": "w1"},
                  {"pane_id": "w1:p1", "workspace_id": "w1"},
                  {"pane_id": "w9:p1", "workspace_id": "w9", "agent": "claude"}],
        "agents": [{"name": "voice-routing-fix", "pane_id": "w9:p1", "agent_status": "working"}],
    })

    out = tools.fleet()
    assert "shell 1 in home" in out and "shell 2 in home" in out
    assert "routing fix is working" in out
    records = tools._speakable_labels()
    assert tools._find_lane("shell one in home", records)["pane_id"] == "w1:p1"
    zoom = tools.fleet({"pane": "shell one in home"})
    assert "Recent:" in zoom and "Process:" in zoom


def test_reuse_claude_clears_adopts_and_sends_prompt_pointer(monkeypatch, tmp_app):
    fake = _install_fake(monkeypatch, tmp_app)
    fake.state.update({
        "workspaces": [{"workspace_id": "w9", "label": "voice"}],
        "panes": [{"pane_id": "w9:p1", "workspace_id": "w9", "agent": "claude", "argv": ["claude"]}],
        "agents": [{"name": "old-lane", "pane_id": "w9:p1", "agent_status": "idle"}],
    })

    spoken = tools.delegate_task("new work", _watched_cfg(), task_name="new-work", reuse_pane="old lane")
    sends = [call for call in fake.calls if call[:2] == ("agent", "send")]
    assert any(call[3] == "/clear" for call in sends)
    assert any(call[3].startswith("Read ") for call in sends)
    assert ("agent", "rename", "w9:p1", "voice-new-work") in fake.calls
    assert not [call for call in fake.calls if call[:2] == ("pane", "run")]
    assert "reused" in spoken.lower()


def test_reuse_bare_shell_runs_promptfile_and_pi_spawns_fresh(monkeypatch, tmp_app):
    fake = _install_fake(monkeypatch, tmp_app)
    fake.state.update({
        "workspaces": [{"workspace_id": "w1", "label": "home"}, {"workspace_id": "w9", "label": "voice"}],
        "panes": [{"pane_id": "w1:p1", "workspace_id": "w1", "argv": ["zsh"]},
                  {"pane_id": "w9:p2", "workspace_id": "w9", "agent": "pi", "argv": ["pi"]}],
        "agents": [{"name": "old-pi", "pane_id": "w9:p2", "agent_status": "idle"}],
    })

    tools.delegate_task("shell work", _watched_cfg(), task_name="shell-work", reuse_pane="shell one in home")
    run = next(call for call in fake.calls if call[:2] == ("pane", "run"))
    assert run[2] == "w1:p1" and "$(cat " in run[3]
    assert ("agent", "rename", "w1:p1", "voice-shell-work") in fake.calls

    fake.calls.clear()
    spoken = tools.delegate_task("pi work", _watched_cfg(), task_name="pi-work", reuse_pane="old pi")
    assert any(call[:2] == ("agent", "start") for call in fake.calls)
    assert not any(call[:2] == ("agent", "rename") and call[2] == "w9:p2" for call in fake.calls)
    assert "can't safely reuse a pi lane" in spoken


def test_adopting_pane_dedupes_voice_lane_names(monkeypatch, tmp_app):
    fake = _install_fake(monkeypatch, tmp_app)
    fake.state.update({
        "workspaces": [{"workspace_id": "w9", "label": "voice"}],
        "panes": [{"pane_id": "w9:p1", "workspace_id": "w9", "agent": "claude"},
                  {"pane_id": "w9:p2", "workspace_id": "w9", "agent": "claude"}],
        "agents": [{"name": "voice-routing-fix", "pane_id": "w9:p1", "agent_status": "idle"},
                   {"name": "old-lane", "pane_id": "w9:p2", "agent_status": "idle"}],
    })

    assert tools._adopt_pane("w9:p2", "routing fix", "123456") == "voice-routing-fix-2"
    lanes = tools._speakable_labels()
    assert tools._find_lane("routing fix", lanes)["pane_id"] == "w9:p1"
    assert tools._find_lane("routing fix 2", lanes)["pane_id"] == "w9:p2"


def _bare_agent_session():
    session = agent.VoiceAgent.__new__(agent.VoiceAgent)
    session._announced = set()
    session._announcing = set()
    session._spoken_tasks = queue.Queue()
    session.live_on = True
    return session


def test_live_completion_queue_marks_only_after_next_turn_and_preserves_undrained(monkeypatch, tmp_app):
    monkeypatch.setattr(live_session, "_grab_context", lambda: "")
    monkeypatch.setattr(live_session, "_grab_screenshot", lambda: "")
    path = os.path.join(config.TASKS_DIR, "123456")
    config.ensure_dirs()
    with open(path + ".out", "w", encoding="utf-8") as f:
        f.write("VERIFIED: done")
    with open(path + ".done", "w", encoding="utf-8"):
        pass

    first = _bare_agent_session()
    spoken = []
    live = live_session.LiveSession(on_task_spoken=first._task_spoken)
    first.live = live
    first._check_tasks(None)
    assert "123456" not in first._announced
    assert not live.offer_task("123456", "duplicate")

    class Ws:
        async def send(self, message):
            spoken.append(message)

    async def drain_once():
        live._loop = asyncio.get_running_loop()
        live._ws = Ws()
        await live._inject_context_and_respond()

    asyncio.run(drain_once())
    assert sum("Task 123456:" in message for message in spoken) == 1
    first._check_tasks(None)
    assert "123456" in first._announced

    second = _bare_agent_session()
    second_live = live_session.LiveSession(on_task_spoken=second._task_spoken)
    second.live = second_live
    second._check_tasks(None)
    assert "123456" not in second._announced
    second.live_on, second.live = False, None
    woke = []
    second._wake_and_speak = lambda text, announce_tid=None: woke.append(text) or True
    second._check_tasks(None)
    assert woke == ["VERIFIED: done"] and "123456" not in second._announced
    assert "123456" in second._announcing
    second._task_spoken("123456")
    second._check_tasks(None)
    assert "123456" in second._announced


def test_live_completion_requeues_when_finished_task_send_fails(monkeypatch):
    monkeypatch.setattr(live_session, "_grab_context", lambda: "")
    monkeypatch.setattr(live_session, "_grab_screenshot", lambda: "")
    spoken, acknowledged = [], []
    live = live_session.LiveSession(on_task_spoken=acknowledged.append)
    assert live.offer_task("123456", "VERIFIED: done")

    class FailingWs:
        async def send(self, message):
            if "Task 123456:" in message:
                raise RuntimeError("connection dropped")

    class WorkingWs:
        async def send(self, message):
            spoken.append(message)

    async def scenario():
        live._loop = asyncio.get_running_loop()
        live._ws = FailingWs()
        try:
            await live._inject_context_and_respond()
        except RuntimeError as exc:
            assert str(exc) == "connection dropped"
        else:
            raise AssertionError("finished-task send should fail")
        assert list(live._offered_tasks.queue) == [("123456", "VERIFIED: done")]
        assert acknowledged == []

        live._ws = WorkingWs()
        await live._inject_context_and_respond()
        await live._inject_context_and_respond()

    asyncio.run(scenario())
    assert live._offered_tids == {"123456"}
    assert sum("Task 123456:" in message for message in spoken) == 1
    assert acknowledged == ["123456"]


def test_foreign_close_stages_above_generic_high_stakes_gate(monkeypatch):
    calls, sent = [], []

    class Ws:
        async def send(self, message):
            sent.append(message)

    monkeypatch.setattr(live_session.config, "activity", lambda _message: None)
    monkeypatch.setattr(live_session, "close_gate", lambda _args: True)
    monkeypatch.setattr(live_session, "close_finished_tasks",
                        lambda _args, confirmed=False: calls.append(confirmed) or "Closed foreign.")

    async def scenario():
        session = live_session.LiveSession()
        session._loop = asyncio.get_running_loop()
        session._ws = Ws()
        await session._do_tool({
            "call_id": "close-1", "name": "close_finished_tasks",
            "arguments": '{"task_name": "foreign"}',
        })
        assert calls == []
        assert session._pending_action["tool"] == "close_finished_tasks"
        assert "CONFIRMATION REQUIRED" in sent[0]
        await session._resolve_pending_action("yes")

    asyncio.run(scenario())
    assert calls == [True]


def test_launch_age_prune_removes_only_sidecars_older_than_seven_days(tmp_app):
    config.ensure_dirs()
    old = os.path.join(config.TASKS_DIR, "010101.done")
    fresh = os.path.join(config.TASKS_DIR, "020202.done")
    for path in (old, fresh):
        with open(path, "w", encoding="utf-8"):
            pass
    eight_days = time.time() - 8 * 24 * 60 * 60
    os.utime(old, (eight_days, eight_days))

    agent._prune_old_tasks()
    assert not os.path.exists(old)
    assert os.path.exists(fresh)
    assert agent._seed_announced_tasks() == {"020202"}


def test_missing_herdr_skill_fails_soft_and_logs_once(monkeypatch, tmp_path):
    logs = []
    monkeypatch.setattr(live_prompt, "HERDR_SKILL_PATH", str(tmp_path / "missing.md"))
    monkeypatch.setattr(live_prompt, "_herdr_skill_warned", False)
    monkeypatch.setattr(live_prompt, "_log", logs.append)
    assert live_prompt._load_herdr_doctrine() == ""
    assert live_prompt._load_herdr_doctrine() == ""
    assert len(logs) == 1
