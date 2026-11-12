# Can a waiting hook carry the phone answer? (issue #21)

Date 2026-11-12. Claude Code 2.1.322 on this Mac. Tags: [TESTED] ran it here, [READ] stated in a primary doc, [INFERRED] my reasoning, not verified.

## Verdict: partly

A waiting command hook is a real answer path for tool permissions (Bash, edits, MCP). It is not yet proven for AskUserQuestion: in `claude -p` that tool is not offered at all without a permission host, and a hook did not change that. Whether the desk dialog stays hidden while the hook waits is not documented and not tested.

## 1. Timeout limits and what happens on timeout

- Default `timeout` for `command`, `http`, `mcp_tool` hooks is 600 s. It is 30 s on UserPromptSubmit and 10 s on MessageDisplay. [READ] https://code.claude.com/docs/en/hooks.md (common fields table).
- The docs state no maximum for `timeout`; 600 s is only the default. [READ, absence of a cap]
- On timeout the hook is cancelled and its output discarded. On PreToolUse a timed-out hook does not block: the call goes on through the normal permission flow. `onFailure: "block"` makes it block instead. [READ] hooks.md, "Timeouts". [TESTED] hook slept 6 s with `timeout: 3`: it was killed and the Bash tool ran normally.
- PermissionRequest timeout: no separate rule in the docs; the general rule "on most events a timed-out hook renders no decision" applies, so the normal prompt remains. [READ + INFERRED]
- The "5 s" in the issue is not a Claude Code limit; it is whatever `timeout` is set in the hook entry. [READ]
- A hook waiting minutes is allowed; async hooks (`async: true`) have no enforced timeout but cannot decide anything. [READ]

## 2. Does the desk prompt show while the hook waits?

- Not stated in the docs for the interactive TUI. [READ: gap]
- PreToolUse runs before the permission flow, so by design the prompt cannot appear until the hook returns. [INFERRED from "falls through to the normal permission flow", hooks.md]
- PermissionRequest runs "when Claude Code is about to ask you"; for the SDK `canUseTool` host the docs say hooks run alongside it and "whichever decides first applies". Whether the TUI dialog is shown during the wait is unknown. [READ + gap]
- Not tested: needs an interactive TUI session; headless cannot show it. A follow-up could run `claude` inside a throwaway tmux session (not a herdr pane).

## 3. What a hook can return

- PreToolUse: `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow|deny|ask|defer","permissionDecisionReason":"...","updatedInput":{...}}}`. `deny` reason is shown to Claude. [READ] hooks.md "PreToolUse decision control". [TESTED] deny with a reason after a 6 s sleep: the model received it as the tool result ("PreToolUse:Bash hook error: PHONE-ANSWER: ..."), so the wait works and the text arrives, but wrapped in "hook error".
- PermissionRequest: `{"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow|deny","updatedInput":{...},"updatedPermissions":[...],"message":"...","interrupt":true}}}`. `message` and `interrupt` are deny-only; `updatedInput` is allow-only. [READ] hooks.md "PermissionRequest decision control".
- AskUserQuestion, exact JSON documented: PreToolUse `allow` plus `updatedInput` holding the original `questions` array and an `answers` object mapping question text to chosen option label (multi-select joined by commas). `allow` alone is not enough. PermissionRequest accepts the same `updatedInput`. [READ] hooks.md "Tools that require user interaction":
  `{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"allow","updatedInput":{"questions":[...unchanged...],"answers":{"Which framework?":"React"}}}}`
- There is also `defer` (only with `-p`, single tool call per turn): ends the turn, caller resumes with `claude -p --resume <id>` and the hook then returns the answer. [READ] hooks.md "Defer a tool call for later". This is the documented no-waiting route for AskUserQuestion in headless runs.
- Deny and ask rules still apply after an `allow`. [READ]
- Not tested: the AskUserQuestion `answers` return (tool unavailable, see 4).

## 4. Headless `claude -p` and cloud/web sessions

- Hooks fire in the terminal, IDE, Desktop app and cloud sessions. [READ] hooks.md intro. Cloud sessions do not read `~/.claude/settings.json`; hooks must come from the repo's `.claude/settings.json` or the environment. [READ] hooks.md and https://code.claude.com/docs/en/cloud-environments.md
- Cloud network is limited by default (None / Limited / Full levels); the inbox host would need to be allowed. [READ] cloud-environments.md "Access levels". Not tested.
- `-p`: AskUserQuestion and ExitPlanMode are offered only when the run has a permission host (e.g. `--permission-prompt-tool` MCP tool, SDK `canUseTool`). The docs also say a PreToolUse hook "satisfies that requirement". [READ] hooks.md.
  [TESTED, contradicts the second sentence in practice] With a project hook on `AskUserQuestion` in `claude -p --model haiku`, the model said the tool was not available and the hook never ran (also with `--tools AskUserQuestion`). So in 2.1.322 headless, a hook alone did not make the tool appear.
- `-p` treats the folder as trusted, so project hooks run. [READ] hooks.md. [TESTED] a project `.claude/settings.json` PreToolUse hook on Bash ran in `-p` and a 6 s wait returned a decision correctly.
- A PermissionRequest hook is the documented fallback in `-p`: with no hook decision a prompt that cannot show is denied. [READ]

## 5. Codex CLI (docs only, not run)

Source: https://developers.openai.com/codex/hooks.md [READ]
- `timeout` is in seconds, default 600 for most hooks. The 1 to 3 s clamp applies only to the turn-interrupt event. No maximum is stated for PreToolUse or PermissionRequest. A PreToolUse error, timeout or malformed response can let the tool run.
- PermissionRequest runs when Codex is about to ask for approval (not for commands that need none). It may return `{"hookSpecificOutput":{"hookEventName":"PermissionRequest","decision":{"behavior":"allow"}}}` or `behavior:"deny"` with `message`. No decision means the normal approval prompt. Any deny wins over allow.
- `updatedInput`, `updatedPermissions` and `interrupt` are not accepted on PermissionRequest ("fail closed today"). PreToolUse may return `updatedInput` with `allow`.
- Codex has no AskUserQuestion-style tool in these docs, so an answer to a free-form question cannot ride this path; only approvals can.

## What this means for PAM Inbox

- Tool approvals (allow/deny): a long-poll hook works in Claude Code (tested wait + deny reason) and Codex (read). Set `timeout` generously, and expect fall-through to the desk prompt if it expires.
- AskUserQuestion: documented return shape is clear, but unproven; the headless gap above needs a test with a real permission host or in an interactive session before relying on it.
- Open: does the desk dialog stay hidden while the hook waits (interactive test needed).
