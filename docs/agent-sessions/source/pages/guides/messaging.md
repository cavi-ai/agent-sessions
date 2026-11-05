# Messaging and claims

## Choose a recipient

Send to a session ID for one recipient:

```sh
agent-sessions send SESSION_ID 'Check the parser'
```

The recipient must be discoverable within the activity window. Use `--minutes`
before the command to widen that window when needed:

```sh
agent-sessions --minutes 120 list
```

Send to an existing directory when any eligible session under that path may take
it, including a session that starts there later:

```sh
agent-sessions send /path/to/project 'Check the parser'
```

Directory messages exclude their sender. They reach each session under the path
once until claimed. Use session delivery for a brief intended for one worker.

## Understand message state

| State | What happened | What to do |
| --- | --- | --- |
| `pending` | No session has received the message | Wait for a hook or run `inbox` in the recipient |
| `received` | A hook or `inbox` showed it to one or more sessions | Claim it before taking the work |
| `claimed` | One session reserved it and removed it from the queue | Inspect the holder with `status`; coordinate with that session |

Preview without marking messages received:

```sh
agent-sessions inbox --keep
```

Read the next messages and reserve one:

```sh
agent-sessions inbox
agent-sessions claim MESSAGE_ID 'ready, taking the review'
```

`claim MESSAGE_ID` uses `ready` as its default reply. A successful claim is final
for that message; later claims report who holds it. A claim cannot be used for a
partner invitation: connect the pair instead.

## Replies

A claim sends a reply to the original sender, referencing the original message.
The sender reads it through a hook or `inbox`. Replies need no separate claim and
are removed when received.

A receipt records that the CLI emitted a message, not that a model acknowledged
it. If a host stops after receipt, the task remains claimable. A claim records the
agent's acknowledgment and ownership of the message.

## Session identity

Codex uses `CODEX_THREAD_ID`; Claude Code uses `CLAUDE_CODE_SESSION_ID`. When the
hosts are nested, the nearer native process determines which identity is used.
Use `--session ID` to select the caller explicitly for commands that accept it.

Without a native identity, `inbox` infers one only when exactly one active session
uses the current directory. If several sessions share a checkout, specify the ID.

## Delivery timing and shared files

Sending never interrupts a session or starts a turn. Delivery waits for the next
supported hook or explicit `inbox` call. Start a coordinated turn with `inbox`
when queued context must precede other work.

`who` compares current Git state and recorded edits. It does not assign file
ownership or lock a checkout. Agree on files before editing them.

See [Hook setup](hooks.md), [CLI reference](../reference/cli.md) and
[the security model](../security/model.md).
