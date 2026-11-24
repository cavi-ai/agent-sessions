# Hook setup

Hooks deliver queued messages inside native host sessions. They also check the
launch configuration of explicitly bound worker profiles.

## Preview and install

Preview only the hosts you need:

```sh
agent-sessions configure codex --dry-run
agent-sessions configure claude --dry-run
agent-sessions configure agy --dry-run
agent-sessions configure opencode --dry-run
```

Install the definitions (`antigravity` is an alias for `agy`):

```sh
agent-sessions configure codex
agent-sessions configure claude
agent-sessions configure agy
agent-sessions configure opencode
```

Configuration preserves unrelated host settings and hooks. Codex and Claude
match this installation’s command when merging or removing handlers. Antigravity
keeps unrelated entries in its named `agent-sessions` group. OpenCode gets one
plugin file that Agent Sessions owns; configuration refuses to replace a file
it did not write. Reconfiguration replaces older Agent Sessions handlers, and
removal matches this installation’s command.

| Host | Configuration file | Registered events |
| --- | --- | --- |
| Codex | `$CODEX_HOME/hooks.json`, default `~/.codex/hooks.json` | `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse` |
| Claude Code | `$CLAUDE_CONFIG_DIR/settings.json`, default `~/.claude/settings.json` | `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse` |
| Antigravity (`agy`) | `$AGY_CONFIG_DIR/hooks.json`, default `~/.gemini/config/hooks.json` | `SessionStart`, `PreInvocation`, `PreToolUse`, `PostToolUse` |
| OpenCode | `$OPENCODE_CONFIG_DIR/plugins/agent-sessions.ts`, default `~/.config/opencode/plugins/agent-sessions.ts` | Session `prompt` (`UserPromptSubmit`), tool `execute.before` (`PreToolUse`), tool `execute.after` (`PostToolUse`) |

## Review and load

In Codex, open `/hooks` or Settings > Hooks to review and trust the exact definitions.
Registration does not grant trust. Run `opencode reload` after configuring
OpenCode. Restart or resume a host if it has not loaded its updated configuration. See the official [Codex hooks documentation](https://learn.chatgpt.com/docs/hooks).

All configured hosts must use the same `AGENT_SESSIONS_DATA` value. If unset,
they use `~/.local/share/agent-sessions`.

## What the hooks do

Startup and prompt hooks deliver messages and identify other sessions in the
same checkout once per peer. Codex and Claude Code also check for messages at
`PostToolUse`. The configured Antigravity hooks deliver at `SessionStart` and
`PreInvocation`. The OpenCode plugin appends prompt-time context to the
submitted prompt and adds post-tool context as a synthetic session message.
An empty inbox produces no message context. Hooks marked with `agent_id`, and
OpenCode child sessions, leave message delivery to the main session.

`PreToolUse` checks bound profiles; it does not deliver messages. Ordinary
messaging errors are reported on stderr without blocking the host. A profile
mismatch fails closed: Codex, Claude Code and OpenCode exit with code 2 (the
OpenCode plugin fails the prompt or tool call), while Antigravity
(`agy`) returns exit code 0 with a structured deny/terminate payload
(`{"decision": "deny", "reason": ...}` on `PreToolUse` or
`"terminationBehavior": "terminate"` on other events).
OpenCode also rejects the hook when its subprocess fails to run or exits
unsuccessfully.

Hosts control when events run. A native app follow-up may reach a later tool hook.
Use `inbox` as the first action when a turn needs queued context before other work.

## Update, move or remove

Hook commands invoke the installation's Python interpreter with `-m agent_sessions`
(reconfiguration also replaces older `agent_sessions.py` handlers). Before moving, updating
or uninstalling an installation, remove its handlers using that installation.
Remove the old installation’s handlers before configuring the new CLI:

```sh
agent-sessions configure codex --remove
agent-sessions configure claude --remove
agent-sessions configure agy --remove
agent-sessions configure opencode --remove
```

If an old installation has already disappeared, inspect the host settings and
remove only its obsolete handlers. Keep other handlers intact. Install the current definitions and repeat Codex’s trust review
as needed.

For missing messages or blocked profiles, see [Troubleshooting](troubleshooting.md).
