# Quickstart

This walkthrough sends a task from one native session to another, then claims it
and sends a reply. Both sessions must use the same message directory. The default
works when both hosts run as the same operating-system user.

## 1. Find the recipient

In the sending session:

```sh
agent-sessions list
agent-sessions who .
```

`list` shows recently active Codex and Claude sessions. `who` narrows the view to
your repository, including linked worktrees. A `SAME WORKTREE` label means another
session uses your checkout. Coordinate file ownership before editing shared files.

## 2. Send a bounded brief

Copy the recipient's ID and replace `SESSION_ID`:

```sh
agent-sessions send SESSION_ID 'Review the parser; report edge cases before changing files'
```

Use a complete ID, or an unambiguous prefix of at least six characters.
The command queues a message; it does not start a turn in the other session.

## 3. Read and claim

In the receiving session:

```sh
agent-sessions inbox
```

Read the brief. If the work is authorized for that session, copy its message ID
and replace `MESSAGE_ID`:

```sh
agent-sessions claim MESSAGE_ID 'ready, reviewing the parser'
```

Only one session can claim a message. If another session already holds it, leave
the work with that session. Receiving a message alone does not claim it.

## 4. Read the reply

In the original sending session:

```sh
agent-sessions inbox
agent-sessions status MESSAGE_ID
```

The reply references the original message. `status` reports its claimant.
Replies are removed from the queue when received; they need no separate claim.

## 5. Enable hooks

Register the hosts you use:

```sh
agent-sessions configure codex
agent-sessions configure claude
```

Review and trust the Codex definitions in `/hooks` or Settings > Hooks.
The next supported hook delivers new messages automatically. For work that must
read queued context first, start the turn with `agent-sessions inbox`.

## Choose the next workflow

- Use a directory recipient for work that can be claimed by a peer under that path:
  [Messaging and claims](../guides/messaging.md).
- Create or attach a partner: [Partner sessions](../guides/partners.md).
- Pin a worker's configuration: [Worker profiles](../guides/profiles.md).
- Resolve setup issues: [Troubleshooting](../guides/troubleshooting.md).
