import json
from types import SimpleNamespace

import kernel_tools
import live_session
import memory


def test_learn_mirrors_learnings_and_ignores_kernel_failure(monkeypatch):
    payload = {
        "summary": "Discussed durable preferences.",
        "new_learnings": [
            {"type": "preference", "text": "Prefers concise answers"},
            {"type": "fact", "text": "Lives in Oslo"},
            {"type": "fact", "text": ""},
        ],
        "supersede": [],
    }
    applied = []
    mirrored = []

    monkeypatch.setattr(memory, "learnings_block", lambda _limit: "")
    monkeypatch.setattr(memory, "set_summary", lambda *_args: None)
    monkeypatch.setattr(memory, "extract_apply", lambda data, cid: applied.append((data, cid)))
    monkeypatch.setattr(
        live_session.subprocess,
        "run",
        lambda *_args, **_kwargs: SimpleNamespace(stdout=json.dumps(payload)),
    )
    monkeypatch.setattr(kernel_tools, "kernel_remember", lambda args: mirrored.append(args))

    session = live_session.LiveSession()
    session._cfg = {"live": {"memory": {"learn": True}}}
    session._learn(7, "Conversation transcript")

    assert mirrored == [
        {"text": "[voice-learned preference] Prefers concise answers"},
        {"text": "[voice-learned fact] Lives in Oslo"},
    ]

    monkeypatch.setattr(
        kernel_tools,
        "kernel_remember",
        lambda _args: (_ for _ in ()).throw(RuntimeError("kernel unavailable")),
    )
    session._learn(8, "Conversation transcript")

    assert [cid for _data, cid in applied] == [7, 8]
