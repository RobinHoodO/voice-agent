---
status: verified
trigger: "Voice agent feels slower and responds slower; review previous changes, identify why, and propose a solution to restore fast response and work."
created: 2026-08-11T11:16:57+02:00
updated: 2026-08-11T12:33:12+02:00
---

## Current Focus

hypothesis: Verified. The ordered Gemini turn boundary restores the pre-regression response path while retaining screen context and preventing the post-tool interruption/reset loop.
test: Done. Commit 15c8b40 was deployed from codex/gemini-instant-context and exercised in the real desktop app; the screen-grounded voice turn and the Notion tool turn were both checked against the retained log, transcript, and action journal.
expecting: Met. First output landed about two seconds after activityEnd with correct screen grounding, no stall nudges, and a single Notion call rather than a repeated loop.
next_action: None outstanding for this investigation. The fix is on the branch and covered by the Gemini lifecycle suite; merge is tracked by the pull request, not by this record.

## Symptoms

expected: The voice agent detects the end of a turn, begins responding, and completes work quickly.
actual: The agent feels slower overall and begins responding more slowly than before.
errors: No explicit error reported.
reproduction: Normal voice-agent use; exact surface and timestamps are not yet known.
started: After unspecified previous changes.

## Eliminated

- Screen capture duration as the primary 15-second delay. The log records `context injected` before the long idle period, and the watchdog is what restarts output.
- Gemini model selection as the regression trigger. `gemini-3.1-flash-live-preview` has been configured since the Gemini layer was introduced on 2026-08-02; the model did not change at the regression boundary.
- VAD tail decay as the current 15-second stall. Commit 8e2aadd independently removed about 1.4 seconds of smoothed-level tail delay after the response-start regression was already present.
- Large instructions/tool schema as the primary regression. They can add baseline inference cost, but the same setup answered quickly immediately before 2d96ae2 and phone turns without late context remain quick afterward.

## Evidence

- timestamp: 2026-08-11T11:16:57+02:00
  checked: Repository status and recent history.
  found: Current branch is voice-convergence; AGENTS.md and CLAUDE.md have pre-existing uncommitted changes. Recent commits include VAD timing changes, duplicate-turn suppression, new tools, phone prompt/audio changes, and loop handling.
  implication: The regression window has multiple plausible latency sources; user-owned working-tree changes must remain untouched.

- timestamp: 2026-08-11T11:16:57+02:00
  checked: GitNexus availability and index.
  found: The MCP calls are not attached, but the repository contains a freshly updated local .gitnexus index and bundled runner.
  implication: GitNexus analysis can proceed through the local runner/graph rather than MCP.

- timestamp: 2026-08-11T11:25:25+02:00
  checked: GitNexus graph status, flow context, and upstream impact.
  found: The index is current at cc2a171. _inject_context_and_respond affects 7 symbols at LOW risk; changing _pump_mic is HIGH risk because it sits in the core _run/session flow; changing _do_tool is HIGH risk and affects both the main run and normalized-event flows.
  implication: The sequencing fix should be made at the narrow orchestration/backend capability seam where possible; microphone-loop or dispatcher changes require broader regression coverage.

- timestamp: 2026-08-11T11:25:25+02:00
  checked: Current runtime configuration and setup payload.
  found: The active backend is Gemini, all cursor/window/screenshot context switches are enabled, manual VAD silence is 1.5 seconds, and the Mac profile sends 49 tools. Instructions plus tool schema are about 49 KB before wire wrapping.
  implication: Nearly every desktop turn injects late context on the affected Gemini path; schema size is a secondary latency contributor, not an explanation for the deterministic 15-second stalls.

- timestamp: 2026-08-11T11:25:25+02:00
  checked: Agent log timing around recent live turns.
  found: Context was injected at 10:57:07, the watchdog nudged at 10:57:22, and the first audio arrived at 10:57:23. The same 15-17 second pattern appears at 21:57:48 -> 21:58:04 -> 21:58:05 and 21:58:17 -> 21:58:33 -> first tool at 21:58:36. Turns without screen context can start audio in about one second after turn close.
  implication: The watchdog, not normal generation, is completing blocked Gemini turns. This is the principal response-start regression.

- timestamp: 2026-08-11T11:25:25+02:00
  checked: Commit 2d96ae2 and current message order.
  found: Manual VAD sends Gemini activityEnd first (which starts generation), then _on_speech_stopped injects screen context with turnComplete=false, and already_replying=True suppresses trigger_response. Commit 2d96ae2 introduced that suppression to remove duplicate greetings.
  implication: The duplicate-reply fix traded two responses for an unfinished late context turn that often waits for the 15-second watchdog.

- timestamp: 2026-08-11T11:25:25+02:00
  checked: Action journal after the cc2a171 loop-guard deployment.
  found: One request executed notion_list_tasks 51 times from 10:55:48 through 10:56:42 (27 Focus calls and 24 In Progress calls), with no guard refusal.
  implication: The production loop guard is not enforcing its six-call ceiling in the real Gemini continuation flow, so read work can still spin for nearly a minute.

- timestamp: 2026-08-11T11:25:25+02:00
  checked: Relevant tests and provider protocol.
  found: All 18 focused tests pass, but the duplicate-reply test stubs screen capture to return no context and the loop test exercises pure counters rather than the event/turn lifecycle. Google's Live API WebSocket example sends toolResponse and then waits for continuation; it does not send a second empty clientContent turn.
  implication: Existing tests prove helpers but miss the message-ordering and state-reset integration failures seen live.

- timestamp: 2026-08-11T12:02:42+02:00
  checked: Clean, commit-aligned historical desktop window in the retained agent log.
  found: The ten context-enabled turns immediately before the 2d96ae2 deployment reached `live heard` in [1, 0, 2, 4, 3, 1, 2, 2, 2, 2] seconds (median 2.0). The next ten paired desktop turns reached it in [16, 17, 17, 16, 15, 1, 2, 17, 17, 17] seconds (median 16.5); eight waited at the watchdog boundary. On 2026-08-11 the five retained desktop turns were [1, 0, 16, 17, 16] seconds (median 16).
  implication: The user's memory is correct: Gemini plus screen context was fast. The immediate step-change at one deployment, plus occasional fast post-change turns, is characteristic of the new message-order race rather than stable provider latency.

- timestamp: 2026-08-11T12:02:42+02:00
  checked: Exact historical code sequence around commit 2d96ae2.
  found: Before the commit the wire sequence was activityEnd -> screen clientContent(turnComplete=false) -> explicit empty clientContent(turnComplete=true). The commit retained the first two messages but suppressed the final completion for Gemini. It solved duplicate replies by removing a second generation request after activityEnd, but left late context capable of interrupting generation without completing a new turn.
  implication: Reverting the commit would restore speed but also restore duplicate answers. The correct fix is one ordered generation boundary, with per-turn context included before it.

- timestamp: 2026-08-11T12:02:42+02:00
  checked: Current official Gemini 3.1 Live API contract.
  found: ClientContent interrupts current generation; turnComplete=false makes the server await more input. Gemini 3.1 supports clientContent only for initial-history seeding and directs ongoing text to realtimeInput. RealtimeInput supports text and JPEG/PNG video frames, and Gemini 3.1's default turn coverage includes audio activity plus all video.
  implication: The durable fix is not merely moving the current clientContent packets. Ongoing screen text and screenshot frames should use realtimeInput inside the explicit activityStart/activityEnd window.

- timestamp: 2026-08-11T12:02:42+02:00
  checked: Tool-response protocol and the 51-call Notion incident.
  found: The official Live API examples send toolResponse and wait for the model continuation. The app instead sends toolResponse and then an empty clientContent trigger. Gemini reports client-message interruptions as `serverContent.interrupted`; the backend maps every such event to SPEECH_STARTED, and `_on_speech_started` clears the per-name read guard.
  implication: The extra post-tool trigger can both interrupt Gemini's normal continuation and erase the guard state, explaining how the deployed six-call ceiling still allowed 51 calls. This mechanism is strongly supported by code, protocol, and timing, but raw interruption events were not logged, so it should be pinned with a protocol/event integration test.

- timestamp: 2026-08-11T12:02:42+02:00
  checked: GitNexus upstream impact for proposed seams.
  found: Gemini backend send_text_context/send_tool_result are LOW-risk graph leaves (dynamic backend dispatch means the graph does not see their callers). `_inject_context_and_respond` is HIGH risk at depth four (8 impacted symbols, `_run` and `_handle_normalized` flows); `_pump_mic` and `_on_speech_started` are HIGH risk in the main run flow; Gemini parse_event is MEDIUM (19 impacted, 7 direct tests).
  implication: Implement provider-specific wire methods at the backend seam, keep the microphone-loop change minimal, and require lifecycle tests for the HIGH/MEDIUM orchestration and event-parser surfaces before rollout.

- timestamp: 2026-08-11T12:33:12+02:00
  checked: Deployed desktop trial on commit 15c8b40 and saved conversation 178.
  found: The first screen-grounded turn sent 2,823 characters before activityEnd and began audio two seconds later; the answer correctly identified Google Maps and directions to Dustin Norway AS. The second turn sent 3,136 characters plus a screenshot before activityEnd and dispatched exactly one notion_list_tasks call two seconds later. There were no 6-second working tones, 15-second nudges, or repeated tool calls. The user reported that the agent is "much faster now."
  implication: The real app has returned from a roughly 16-second median response boundary to the historical roughly 2-second behavior, an approximately 8x response-start improvement, while screen grounding and tool dispatch remain active.

- timestamp: 2026-08-11T12:33:12+02:00
  checked: Regression and repository verification for the committed branch.
  found: The dedicated Gemini lifecycle suite covers ordered context/activityEnd, interruption semantics, tool continuation, and guard persistence; the full repository test suite, compileall, and git diff checks pass. GitNexus staged change detection reported only the seven intended implementation/test files, 29 symbols, and ten related execution processes.
  implication: Existing provider behavior is protected by tests, and the deployed patch matches the reviewed scope.

## Resolution

root_cause: Commit 2d96ae2 suppresses Gemini's explicit completion after activityEnd while the desktop still appends screen context afterward as clientContent(turnComplete=false). Because clientContent interrupts generation, the new order races: a few turns answer before the late context takes effect, but most are interrupted and left incomplete until the 15-second stall nudge. Separately, every Gemini tool result is followed by another clientContent trigger; that can interrupt normal tool continuation, is misclassified as user speech, and clears the six-call read guard.
fix: Keep Gemini and screen context. Prefetch the screen asynchronously at real user speech start; before activityEnd, send the ready text as realtimeInput.text and screenshot as realtimeInput.video, with a small bounded final wait and graceful context skip if capture is still late. Then send exactly one activityEnd and no clientContent generation trigger. For tools, send Gemini toolResponse alone and wait for continuation; retain response.create behavior only for providers that require it. Treat Gemini `interrupted` as response interruption, not a new user utterance, and keep a hard per-user-turn tool budget keyed to an application turn id. Add stage timing and exact wire/event lifecycle tests.
verification: Commit 15c8b40 is deployed on the test branch. A real Gemini Live self-test produced audio, the full repository suite passes, and the real desktop trial answered the screen-grounded turn two seconds after activityEnd with the correct Google Maps context. The subsequent Notion request dispatched one tool call after two seconds with no stall or loop. The session was manually stopped three seconds after tool dispatch, so the automated lifecycle suite remains the evidence for post-tool spoken continuation in this particular trial.
files_changed:
  - core/audio_core.py
  - core/backends/base.py
  - core/backends/gemini_backend.py
  - core/gemini_client.py
  - core/live_session.py
  - tests/test_gemini_backend.py
  - tests/test_gemini_instant_turns.py
