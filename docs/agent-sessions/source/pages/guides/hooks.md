# Hook setup

Hooks deliver queued messages inside native host sessions. They also check the
launch configuration of explicitly bound worker profiles.

## Preview and install

Preview only the host you need, or both:

```sh
agent-sessions configure codex --dry-run
agent-sessions configure claude --dry-run
```

Install the definitions:

```sh
agent-sessions configure codex
agent-sessions configure claude
```

Configuration merges this installation's handlers and preserves unrelated host
settings and hooks. Repeating it does not add duplicate handlers for that installation.

| Host | Configuration file | Registered events |
| --- | --- | --- |
| Codex | `$CODEX_HOME/hooks.json`, default `~/.codex/hooks.json` | `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse` |
| Claude Code | `$CLAUDE_CONFIG_DIR/settings.json`, default `~/.claude/settings.json` | `SessionStart`, `UserPromptSubmit`, `PreToolUse`, `PostToolUse` |

## Review and load

In Codex, open `/hooks` or Settings > Hooks to review and trust the exact definitions.
Registration does not grant trust. Restart or resume a host if it has not loaded
its updated configuration. See the official [Codex hooks documentation](https://learn.chatgpt.com/docs/hooks).

Both hosts must use the same `AGENT_SESSIONS_DATA` value. If unset, they use
`~/.local/share/agent-sessions`.

## What the hooks do

Startup and prompt hooks deliver messages and identify other sessions in the
same checkout once per peer. Tool hooks deliver new messages as the host runs.
An empty inbox produces no message context. Claude subagent hooks leave delivery
to the main session, so a subagent does not consume the main thread's messages.

`PreToolUse` checks bound profiles; it does not deliver messages. Ordinary
messaging errors are reported on stderr without blocking the host. A profile
mismatch blocks prompts and supported tools with exit code 2.

Hosts control when events run. A native app follow-up may reach a later tool hook.
Use `inbox` as the first action when a turn needs queued context before other work.

## Update, move or remove

Hook commands contain absolute Python and module paths. Run `configure` again
after updating. Before moving or uninstalling an installation, remove its handlers
using that installation:

```sh
agent-sessions configure codex --remove
agent-sessions configure claude --remove
```

If an old installation has already disappeared, inspect the host settings and
remove only the obsolete agent-sessions command. Keep other handlers intact.
Install the current definitions and repeat Codex's trust review as needed.

For missing messages or blocked profiles, see [Troubleshooting](troubleshooting.md).
