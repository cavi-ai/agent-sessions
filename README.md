# agent-sessions

Lists the Codex and Claude Code sessions active on this machine, with each one's working directory, worktree, branch, uncommitted files and turn state. It also shows which sessions share a repository and leaves messages that a session's hook delivers. It is one Python 3.9+ file with no dependencies.

## Usage

```sh
agent_sessions.py list                 # active sessions, newest first
agent_sessions.py who [PATH]           # sessions in PATH's repository, any worktree
agent_sessions.py send TO 'text'       # TO: a session id (6+ characters) or a path
agent_sessions.py inbox [--session ID] # unread messages for a session
agent_sessions.py hook < payload.json  # hook entry point for Claude Code and Codex
```

`--minutes N` sets the activity window (default 30). `--json` prints machine-readable output. `list --reviews` adds Codex approval-review threads, which are hidden by default.

`who` marks a session `SAME WORKTREE` when it works in the same checkout as PATH. It also lists that session's uncommitted or recently edited files that PATH has uncommitted too.

A message sent to a path goes to every active session whose working directory is inside that path. Sending never interrupts a session: the message waits until that session's hook runs or its agent runs `inbox`.

## Sources

| Host | Read from |
|---|---|
| Codex | `threads` in `$CODEX_HOME/state_<n>.sqlite` (read-only), and the end of each thread's rollout file for its turn state and edited files |
| Claude Code | `$CLAUDE_CONFIG_DIR/projects/*/<session>.jsonl`, active by modification time |
| Git | `git rev-parse` and `git status --porcelain` in each session's working directory, run at the time of the call |

Messages and delivery records live in `$AGENT_SESSIONS_DATA` (default `~/.local/share/agent-sessions`).

## Hooks

The hook reads the payload that Claude Code and Codex both send (`session_id`, `cwd`, `hook_event_name`). It delivers each unread message once. On `SessionStart` and `UserPromptSubmit`, it also names other active sessions in the same checkout, once per session. It prints nothing when there is nothing new, and it never blocks.

Claude Code, `~/.claude/settings.json`:

```json
{"hooks": {
  "UserPromptSubmit": [{"hooks": [{"type": "command", "command": "/usr/bin/python3 <path>/agent_sessions.py hook", "timeout": 10}]}],
  "PostToolUse": [{"hooks": [{"type": "command", "command": "/usr/bin/python3 <path>/agent_sessions.py hook", "timeout": 10}]}]
}}
```

Codex, `~/.codex/hooks.json`: add the same command under `UserPromptSubmit` and `PostToolUse`.

## Tests

```sh
python3 -m unittest discover -s tests
```
