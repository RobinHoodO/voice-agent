"""PII tools, and the model pin that follows them. Ported from voice-bridge.

`server.py:108` carried `PII_TOOLS`: once a turn touched one of them, the follow-up LLM
call was pinned to a no-train model. Those tools return THIRD-PARTY personal data —
Robin's CRM contacts, his inbox, his LinkedIn graph, the community graph built out of
group chats. The people in that data never agreed to anything, so their names cannot
land in a training corpus because a router happened to pick a cheap provider that day.

Two things the bridge taught, both encoded here:

1. **Never a fan-out combo.** The bridge routed through OmniRoute, which picks a
   provider per call. A turn that touched a PII tool must go to ONE NAMED model with a
   no-train agreement — never `auto/*`, never a combo, because "which sub-processor saw
   this" has to have an answer.

2. **Port the intent, not the string.** The bridge's pin was `router-safe`, a combo that
   stopped existing at the 2026-07-30 OmniRoute cutover. For ~10 days nobody was
   protected by a working pin — they were protected by an HTTP 400. `require_no_train`
   therefore validates the model it is GIVEN and refuses, rather than substituting a
   name that may have rotted. Fail closed, loudly.

Today both live backends (OpenAI Realtime, Gemini Live) are direct named
sub-processors, so there is no fan-out to defend against and this module is a
tripwire rather than a hot path. It exists now because the cheap turn-based fallback
in `docs/BRIDGE-DECOMMISSION.md` (row 1) routes through OmniRoute, and the day that
ships is the day the control has to already be here — the bridge's version was written
after the router, and that is how it rotted unnoticed.
"""
from __future__ import annotations

# Tools that can return personal data about someone who is not Robin.
PII_TOOLS = frozenset({
    "twenty_search_contacts",   # CRM: named people, their companies, their email
    "list_inbox_items",         # inbound mail/SMS/Beeper/LinkedIn from real senders
    "semsearch_query",          # LinkedIn connections corpus
    "kernel_recall",            # ONE memory — CRM + tasks + wiki, names included
    "cognee_ask",               # community graph built from group chats
    "front_search",             # client email threads
    "gmail_search",             # Robin's mailbox, i.e. other people's words
    "notion_search",            # workspace pages, often client material
    "drive_search",             # documents, often client material
    "hybrid_rag_search",        # system graph spanning all of the above
})

# A model id that names ONE sub-processor is fine. These prefixes name a ROUTER that
# picks one at call time — exactly what must not happen after a PII tool.
FAN_OUT_PREFIXES = ("auto/", "auto:", "auto-", "router/", "router-", "combo/", "combo-")


class PiiRoutingRefused(RuntimeError):
    """A turn that saw third-party data was about to be sent somewhere unnamed."""


def touches_pii(tool: str) -> bool:
    return tool in PII_TOOLS


def is_fan_out(model: str | None) -> bool:
    """True when this id does not name a single sub-processor. Empty counts: 'whatever
    the router defaults to' is the thing being refused."""
    if not model or not str(model).strip():
        return True
    name = str(model).strip().lower()
    return name.startswith(FAN_OUT_PREFIXES)


def require_no_train(model: str | None, *, pii_touched: bool) -> str:
    """The model id to use, or a refusal.

    Called by any relay that sends conversation content to a chosen model. When the turn
    has touched a PII tool the id must name one sub-processor; otherwise it is returned
    unchanged. Refusing is the correct outcome — the bridge failed closed for ten days
    and that was the reason nothing leaked.
    """
    if not pii_touched:
        return str(model or "")
    if is_fan_out(model):
        raise PiiRoutingRefused(
            f"this turn used a tool that returns other people's data, so it cannot be "
            f"routed through {model or 'an unnamed default'} — pin a single named "
            f"no-train model")
    return str(model)
