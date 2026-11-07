# CLI reference

```text
agent-sessions [--minutes N] [--json] COMMAND
```

Global options precede the command. `--minutes` changes the discovery window
(default 30); `--json` selects machine-readable output where available. Profile
and partner operations already print structured results.

For exact positional arguments and options, use `agent-sessions COMMAND --help`.

## Discovery

| Command | Purpose |
| --- | --- |
| `list` | List recent sessions, newest first |
| `list --reviews` | Include Codex approval-review threads |
| `who [PATH]` | Show sessions in a repository, including linked worktrees; PATH defaults to `.` |

`SAME WORKTREE` means the same checkout. Overlapping paths compare uncommitted
files and recorded edits; they do not establish file ownership or a live process.

```sh
agent-sessions --json --minutes 120 who /path/to/project
```

## Messaging

| Command | Purpose |
| --- | --- |
| `send TO 'text' [--session ID]` | Queue a message for a discovered session ID or existing directory |
| `inbox [--session ID]` | Receive messages for a session |
| `inbox --keep [--session ID]` | Preview without marking received |
| `claim MESSAGE_ID ['reply'] [--session ID]` | Reserve a message; reply defaults to `ready` |
| `status MESSAGE_ID` | Report pending, received or claimed state and its receivers or holder |

A session ID prefix must be unambiguous and at least six characters. Receiving
is not claiming. Replies reference their original message and need no claim.
See [Messaging and claims](../guides/messaging.md).

## Hook configuration

| Command | Purpose |
| --- | --- |
| `configure codex` | Merge this installation's handlers into Codex settings |
| `configure claude` | Merge this installation's handlers into Claude settings |
| `configure HOST --dry-run` | Print the merged settings without writing |
| `configure HOST --remove` | Remove this installation's handlers, preserving other settings |
| `hook [--host codex\|claude]` | Read one native hook payload as JSON on stdin |

`hook` is a host entry point. It uses `session_id` (or `thread_id`), `cwd` and
`hook_event_name`; native transcript and subagent fields help route delivery.
Configured commands supply the host explicitly. Profile mismatches exit 2;
ordinary message-hook errors report a diagnostic and return 0.

See [Hook setup](../guides/hooks.md).

## Partners

| Command | Purpose |
| --- | --- |
| `partner claude PATH 'brief'` | Queue an invitation for a later Claude session |
| `partner codex PATH 'brief' --project-id ID` | Save a native app creation request |
| `partner HOST PATH 'brief' --profile PROFILE_ID` | Prepare a guarded profile launch |
| `partner connect PAIR_ID [SESSION_ID] [--host-id local]` | Connect a discovered native endpoint; session defaults to the caller |
| `partner attach SESSION_ID [--profile PROFILE_ID]` | Record an existing worker |
| `partner show PAIR_ID` | Inspect a saved pair and its request or launch state |
| `partner resume PAIR_ID [--dry-run]` | Resume the connected partner with its pinned profile |
| `partner run PAIR_ID [--prompt-file FILE] [--timeout SECONDS] [--no-mcp]` | Own one Codex turn with direct profile and capability checks; `--no-mcp` rejects enabled MCP servers; timeout defaults to 60 seconds |

Creation, connection, attachment and resume accept `--session ID` to identify the
caller explicitly. `partner resume` requires a connected, profiled partner.
Ordinary native chat resume remains the host's operation.

See [Partner sessions](../guides/partners.md).

## Profiles

| Command | Purpose |
| --- | --- |
| `profile create HOST PATH NAME [--file RELATIVE_PATH]` | Pin required native files and any extra relative files |
| `profile show PROFILE_ID` | Validate and inspect a saved profile |
| `profile launch PROFILE_ID [--pair PAIR_ID] [--dry-run]` | Start a native host through the guarded launcher |
| `profile launch PROFILE_ID --resume SESSION_ID [--dry-run]` | Resume a bound native session |

Repeat `--file` to pin multiple extra files. A real launch replaces the CLI process
with the native host. A dry run prints its command, working directory and profile
marker without starting it.

See [Worker profiles](../guides/profiles.md) for required files, credential handling
and the limits of profile checks.

`agent-sessions protocol` reports supported managed receipt contracts without starting a host.

## Resource control

`agent-sessions resource` returns compact JSON without scanning host transcripts
or receiving peer messages. Its subcommands are `protocol`, `request RESOURCE KEY
[--seconds N]`, `status TICKET`, `wait TICKET [--seconds N]`, `start TICKET --lease
TOKEN --fence N`, `cancel TICKET`, `finish TICKET --lease TOKEN --fence N --outcome
completed|failed|unresolved [--evidence SHA256]`, and `events [--after CURSOR]`.
`resource --session OWNER` retains a supervised recovery's logical identity.

The API is for resident resource adapters; separate short-lived shell invocations
are not a native lifecycle supervisor. Admission binds the parent PID and kernel
birth identity. Wait deadlines are 0.001–3600 seconds; the queue holds at most 256
pending tickets. A running operation is never expired to free its resource.
See [queued resources](../guides/resources.md) for lifecycle and recovery rules.
