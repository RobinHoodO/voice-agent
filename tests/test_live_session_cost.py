from types import SimpleNamespace

import kernel_tools
import live_session
import memory


def test_persist_logs_realtime_cost_and_ignores_kernel_failure(monkeypatch):
    logged = []

    monkeypatch.setattr(memory, "record", lambda _transcript: 0)
    monkeypatch.setattr(live_session.time, "time", lambda: 130.0)
    monkeypatch.setattr(
        kernel_tools,
        "session_log",
        lambda source, cost_nok, duration_sec, summary: logged.append(
            SimpleNamespace(source=source, cost_nok=cost_nok,
                            duration_sec=duration_sec, summary=summary)),
    )

    session = live_session.LiveSession()
    session._wall_start = 10.0
    session._turns = ["you: Check the deployment cost"]
    session._persist_conversation()

    assert logged[0].source == "voice-agent-realtime"
    assert logged[0].cost_nok > 0
    assert logged[0].duration_sec == 120.0
    assert logged[0].summary == "you: Check the deployment cost"

    monkeypatch.setattr(
        kernel_tools,
        "session_log",
        lambda *_args: (_ for _ in ()).throw(RuntimeError("kernel unavailable")),
    )
    session._persist_conversation()
