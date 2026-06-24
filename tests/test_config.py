"""Harness-proof + config/memory smoke tests.

Proves the tmp_app fixture isolates real storage and that the two foundational
modules (config, memory) work end-to-end against a tmp dir.
"""
import config
import memory


def test_deep_merge_preserves_siblings_and_nested_defaults():
    merged = config._deep_merge(config.DEFAULTS, {"live": {"voice": "echo"}})
    assert merged["live"]["voice"] == "echo"            # override applied
    assert merged["live"]["agentic_shell"] is False     # sibling default kept
    assert merged["live"]["memory"]["recall_count"] == 5  # nested default kept


def test_config_roundtrip_isolated(tmp_app):
    config.set_("live.voice", "shimmer")
    assert config.get("live.voice") == "shimmer"
    # written under the tmp dir, not real ~/Library
    assert (tmp_app / "config.json").exists()


def test_memory_record_and_recall(tmp_app):
    cid = memory.record("hello world transcript", summary="a greeting")
    assert cid > 0
    block = memory.recall("hello")          # FTS matches the transcript...
    assert "greeting" in block.lower()      # ...and recall surfaces the summary line
