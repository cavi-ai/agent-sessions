# agent-sessions

Local discovery and two-way messaging for Codex and Claude Code sessions. See
which sessions share a repository, their checkout, branch and uncommitted files,
then leave a message that the receiving session reads through a hook or its inbox.

Requires Python 3.9+, Git, and macOS or Linux. The runtime has no third-party
dependencies. Windows is not supported because message storage uses POSIX locks.

## Install

From a checkout:

```sh
python3 -m pip install .
agent-sessions --help
```

Alternatively, run `python3 /path/to/agent-sessions/agent_sessions.py` directly.

## Usage

```sh
agent-sessions list                 # recently active sessions, newest first
agent-sessions who [PATH]           # sessions in PATH's repository, any worktree
agent-sessions send TO 'text'       # TO: a session id (6+ characters) or a directory
agent-sessions inbox [--session ID] # consumes unread messages for a session
agent-sessions inbox --keep         # preview without consuming
```

`--minutes N` sets the activity window (default 30). `--json` prints
machine-readable output. Put these options before the subcommand.
`list --reviews` includes Codex approval-review threads.

Codex identifies the caller with `CODEX_THREAD_ID`; Claude Code uses
`CLAUDE_CODE_SESSION_ID`. An explicit `--session ID` overrides either. If neither
variable is set, `inbox` infers identity only when exactly one active session
uses the current directory. When both are set, Codex's identity takes precedence.

`who` marks sessions in the target checkout `SAME WORKTREE`. Its overlap list
compares checkout-level uncommitted paths and recently recorded edits; it does
not prove which session owns an edit. Activity is inferred from recent host
records, not process liveness. Coordinate before editing shared files.

Send to a session ID for one recipient. A message sent to a directory reaches
each session under that directory, **including sessions that start there later**,
excluding the sender. Path messages persist until their inbox file is removed.

## Enable delivery in both hosts

Preview the merged configurations, then register the hooks:

```sh
agent-sessions configure codex --dry-run
agent-sessions configure claude --dry-run
agent-sessions configure codex
agent-sessions configure claude
```

Configuration preserves unrelated settings and hooks. Running it again does not
add duplicates. It registers `SessionStart`, `UserPromptSubmit`, and `PostToolUse`
in `$CODEX_HOME/hooks.json` and `$CLAUDE_CONFIG_DIR/settings.json`, defaulting to
`~/.codex` and `~/.claude`. Commands quote the absolute Python and module paths;
keep the installation there, or remove the hooks before moving it.

In Codex, review and trust the new definitions using CLI `/hooks` or desktop
Settings > Hooks. Registration does not grant trust. See the official
[Codex hooks documentation](https://learn.chatgpt.com/docs/hooks).
Restart or resume the host if it has not loaded the new configuration.

To remove this installation's handlers while keeping other settings:

```sh
agent-sessions configure codex --remove
agent-sessions configure claude --remove
```

The command hook accepts a JSON object on stdin with `session_id` (or `thread_id`),
`cwd`, and `hook_event_name`. It returns JSON `additionalContext` when there are
new messages. Startup and prompt hooks also identify other sessions in the same
checkout once per peer. Empty inboxes produce no output. Hook errors are
reported on stderr and exit successfully so the host can continue.
Configured commands include `hook --host codex` or `hook --host claude` so partner
invitations reach the requested host. Older commands can infer the host from
the transcript path or matching native session environment variable. If the
host is unknown, ordinary messages still work and partner invitations wait.

Sending queues a message; it does not interrupt an agent or start an idle turn.
Delivery records prevent repeat reads, including after more than 500 messages.
They record consumption by this tool, not acknowledgment by the host or model.
A crash after recording consumption can lose the corresponding hook output.

For a manual round trip, have Codex send to the Claude session ID, let Claude's
next hook or `inbox` read it, then reply to the message's `from_id`. Codex's next
hook or `inbox` reads the reply. Both hosts must use the same message directory.

## Partner sessions

```sh
agent-sessions partner claude /path/to/project 'Review the parser'
agent-sessions partner codex /path/to/project 'Review the parser' --project-id PROJECT_ID
agent-sessions partner connect PAIR_ID THREAD_ID --host-id local
agent-sessions partner show PAIR_ID
```

Both commands save a pair record under the message directory. `list` and `who`
show the partner's host, session ID and directory from either end. Pending pairs
show their state without inventing a recipient. `partner show` reads the saved
record, including any pending creation request. Pair records are local routing
metadata; they do not start a daemon or grant account access.
Unreadable pair records report a diagnostic and are skipped during discovery;
an invitation with missing or damaged pair metadata does not block other messages.

For **Claude**, the command queues the brief as a path invitation and reports
`waiting`. It does not launch Claude. The next Claude session under that directory
reads the brief through its hook or inbox and runs the included `partner connect
PAIR_ID` command, which uses its native session identity. The first valid
connection claims the invitation; later sessions no longer receive it.
If queueing fails, the saved record remains `needs_delivery` with its invitation.
Inspect it with `partner show` and resolve the storage error before trying again;
do not assume that a failed append left no message behind.

For **Codex**, creation is an agent workflow using the desktop app's tools, not
a standalone shell operation. The CLI reports `needs_creation` and returns the
exact `mcp__codex_app__create_thread` arguments. The agent must:

1. Call `mcp__codex_app__list_projects` with `{}` and select a saved **local**
   project whose path equals the requested directory. Pass its `projectId` to
   the CLI. A directory outside saved projects cannot be created through this route.
2. Execute the returned native creation request. Its arguments have this shape:

   ```json
   {
     "target": {
       "type": "project",
       "projectId": "PROJECT_ID",
       "environment": {"type": "local"}
     },
     "prompt": "THE_RETURNED_PROMPT"
   }
   ```

3. Use the returned real `threadId` and `hostId` to run `partner connect PAIR_ID
   THREAD_ID --host-id local`. Connection checks the discovered session's host
   and directory before reporting `connected`. The new partner can also connect
   itself with `partner connect PAIR_ID`. Both forms are idempotent for that session.

Creation uses the existing checkout and configured model. It does not create a
worktree. If creation fails or its result is uncertain, leave the request pending
and inspect the app before retrying; a repeated creation call can create a second
chat. A `clientThreadId` from pending app setup is not a usable `threadId`.

For an explicitly authorized native follow-up to the created Codex chat, use
`mcp__codex_app__send_message_to_thread` with these exact argument names:

```json
{"threadId": "THREAD_ID", "hostId": "local", "prompt": "FOLLOW_UP_TEXT"}
```

Native creation and follow-ups are visible in the user's chat sidebar and require
the user's authorization. A peer invitation does not supply that authorization.
The existing `send` command still queues messages for hooks without starting a turn.

## Data and privacy

| Source | Read from |
|---|---|
| Codex | `threads` in `$CODEX_HOME/state_<n>.sqlite`, read-only, and the tail of each thread's rollout for turn state and edited files |
| Claude Code | `$CLAUDE_CONFIG_DIR/projects/*/<session>.jsonl`, active by modification time |
| Git | Live checkout, branch and uncommitted paths from `git rev-parse` and `git status` |

Host databases and transcript formats are implementation details and can change.
Missing hosts are allowed; unreadable Codex databases report a diagnostic.
Claude sessions are marked active by recent transcript modification, not a
running/idle signal.

Messages, pair records and consumption records live in `$AGENT_SESSIONS_DATA`, defaulting to
`~/.local/share/agent-sessions`. Keep this directory outside the checkout and
configure the same value in both hosts. New files are owner-only, plaintext and
retained until removed. Discovery output includes session titles and local
paths; do not publish it. This tool makes no network requests or account logins,
but hook context is passed to the receiving host and model.
See [security and privacy](SECURITY.md) for trust boundaries and retention.

## Development

```sh
python3 -m unittest discover -s tests
```

The suite exercises the CLI, real temporary Git repositories and message files,
with synthetic Codex and Claude host records. It does not establish that either
host has loaded or trusted hooks in a live session. See [contributing](CONTRIBUTING.md)
for lint and package-build commands. Licensed under [MIT](LICENSE).
