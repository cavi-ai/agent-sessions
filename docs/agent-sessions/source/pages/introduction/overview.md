# Overview

Agent Sessions helps Codex, Claude Code, and Antigravity (`agy`) sessions
coordinate on one machine. It discovers recent sessions, compares their Git
checkouts, and passes messages through a local queue. It also records partner
relationships and pins a worker's native launch configuration and starting
context.

## What you can do

| Task | Start here |
| --- | --- |
| Find peers before editing a shared repository | `agent-sessions who .` |
| Ask one session to take work | `agent-sessions send SESSION_ID 'brief'` |
| Leave work for a session under a directory | `agent-sessions send /path/to/project 'brief'` |
| Reserve a received message and reply | `agent-sessions claim MESSAGE_ID 'ready'` |
| Set up Codex, Claude, and Antigravity partners | [Partner sessions](../guides/partners.md) |
| Control starting context and pin worker configs | [Worker profiles](../guides/profiles.md) |

## How coordination works

1. **Discover.** Read recent host records and current Git state. `who` identifies
   the same checkout and overlapping uncommitted paths.
2. **Send.** Queue a brief for a session or directory. Sending does not start a
   model turn or interrupt the recipient.
3. **Receive.** A hook or `inbox` shows each session a message once.
4. **Claim.** One session reserves the message and sends a reply to its sender.

A received message is peer context. It does not grant permission to edit files,
access accounts or execute commands. See the [security model](../security/model.md).

## Sessions, partners and profiles

A **session** is a discovered native Codex, Claude Code, or Antigravity (`agy`)
conversation. Recent activity is inferred from host records; a listed session is
not necessarily running.

A **partner** is a recorded relationship between two sessions. One caller can
have several Codex, Claude Code, and Antigravity partners. Creating an ordinary
Claude or Antigravity partner queues a path invitation; creating an ordinary
Codex partner uses the Codex app's native tools. [Partner sessions](../guides/partners.md)
explains both routes.

A **profile** pins explicitly supplied configuration files and a directory so
each worker starts with only the tools, custom agent settings, and MCP servers
it needs. In multi-agent fanouts, stripping inherited global schemas prevents
context rot and dramatically reduces token usage; see the
[measured 5-agent fanout benchmarks](../guides/profiles.md#measured-5-agent-fanout-benchmarks).
Profiles check launch context and configuration hashes; they do not authenticate
workers or establish which tools a host actually exposes.

## Next steps

[Install the CLI](installation.md), then follow the [quickstart](quickstart.md).
This documentation covers version {{PRODUCT_VERSION}}.
