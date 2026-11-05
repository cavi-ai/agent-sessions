# Troubleshooting

Start with read-only inspection. `inbox --keep`, `status` and `partner show` help
separate a queued message from work already claimed by another session.

## No sessions appear

```sh
agent-sessions list
agent-sessions --minutes 120 list
agent-sessions who /path/to/project
```

The default activity window is 30 minutes. Discovery uses native host records,
not a process monitor. Archived Codex threads are excluded, and approval-review
threads appear only with `list --reviews`.

Check that `CODEX_HOME` and `CLAUDE_CONFIG_DIR` point to the host data you expect.
Missing hosts are allowed. A missing or unreadable database is different from a
session having no recent activity.

## A message has not arrived

1. In the sender, inspect `agent-sessions status MESSAGE_ID`.
2. In the recipient, run `agent-sessions inbox --keep`.
3. Check that both hosts use the same `AGENT_SESSIONS_DATA` directory.
4. Inspect `configure HOST --dry-run` and the host's loaded hook definitions.

Sending does not start a turn. A host may deliver at a later tool hook, so run
`inbox` first when the next action depends on queued context. If `status` says the
message is claimed, coordinate with the recorded holder rather than taking it again.

## The CLI cannot choose a session

Native identity variables normally select the caller. If they are unavailable
and several sessions share the current directory, pass the exact session ID:

```sh
agent-sessions inbox --session SESSION_ID
agent-sessions claim MESSAGE_ID --session SESSION_ID
```

Get the ID from the host or `list`. Do not infer it from a directory shared by
several sessions.

## A Claude invitation is missing

Ordinary Claude invitations reach sessions that start after the pair is created.
A resumed, compacted or already running session is not a new recipient. Check
`partner show PAIR_ID`, install the hooks, then start a new session under the
invited directory. To record an existing worker, use `partner attach SESSION_ID`.

A connected invitation has already been claimed. Do not attempt to claim it as
an ordinary message.

## Native creation or launch has an uncertain outcome

Inspect the pair and the native host before trying again:

```sh
agent-sessions partner show PAIR_ID
```

`needs_creation` records an ordinary Codex app request. `profile_launch_pending`
records an attempted guarded launch. Neither state proves that a new session did
not start. Repeating creation can produce a second worker. Connect an existing
valid result or resume a bound profile instead.

## A profile blocks a prompt or tool

A profile error exits 2 deliberately. Check the directory, pinned files, binding
and native hook trust. Preview the guarded resume:

```sh
agent-sessions partner resume PAIR_ID --dry-run
agent-sessions partner resume PAIR_ID
```

Do not clear a binding or disable hooks to continue a worker under different
configuration. If its intended configuration changed, create and use a new
profile deliberately. Keep the previous session and binding until its work is resolved.

Inline MCP `env`, `http_headers` and `bearer_token` values cannot be passed as
Codex capability overrides. Use the environment references described in
[Worker profiles](profiles.md).

## Legacy queue migration stops

An unreadable delivery-state file stops migration before queue publication or
legacy-folder changes. Preserve the file and the earlier inboxes. Repair the state
from an authoritative source, then retry. Deleting it can make received work look
new; missing delivery history cannot be reconstructed from the queue alone.

An unreadable pair record is diagnosed and skipped. Unrelated sessions and
ordinary messages remain available.

## A direct script reports a missing Python dependency

On Python 3.9 or 3.10, `tomli` is required. Use the package installation described
in [Installation](../introduction/installation.md), or install the checkout's
project dependencies before running `agent_sessions.py` directly.
