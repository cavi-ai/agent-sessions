# Agent Sessions

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

See which Codex and Claude Code sessions share a repository, send work to a peer,
and claim messages before acting on them. Partner records connect sessions across
both hosts; optional profiles preserve a worker's native configuration on resume.

Discovery and messaging run locally. Session titles, project paths and queued
messages stay in local storage until a host passes message context to its model.

## Install

Requires Python 3.9+, Git, and macOS or Linux.

```sh
uv tool install git+https://github.com/cavi-ai/agent-sessions.git
```

For a virtual environment, a source checkout, or upgrading an existing install,
see [Installation](docs/agent-sessions/source/pages/introduction/installation.md).

## Quickstart

Find the sessions in your repository:

```sh
agent-sessions list
agent-sessions who .
```

Copy a recipient's session ID from the output, replacing `SESSION_ID` below:

```sh
agent-sessions send SESSION_ID 'Review the parser and report the edge cases'
```

In the receiving session, read the queue and replace `MESSAGE_ID` with the ID shown:

```sh
agent-sessions inbox
agent-sessions claim MESSAGE_ID 'ready, reviewing the parser'
```

A claim reserves the message for one session and replies to the sender. A directory
can also be the recipient: the message waits for sessions under that path until
one claims it. Reading a message alone does not reserve it.

## Optional automatic delivery

For automatic queued-message delivery, register hooks for the hosts you use:

```sh
agent-sessions configure codex
agent-sessions configure claude
```

Existing settings and other hooks are preserved. Review and trust the Codex hooks
in `/hooks` or Settings > Hooks. Sending does not interrupt a session or start a
turn; messages arrive at the next supported hook or explicit `inbox` call.

See [Hook setup](docs/agent-sessions/source/pages/guides/hooks.md) for previews,
trust review, updates and removal.

Managed Codex `partner run` validates its pinned configuration, effective tools
and history directly. Workers disable apps, plugins, hooks, automatic skills and
workspace memory, and start without inherited coordinator turns. Mandatory host
and project instructions remain applicable. These checks establish context and
tool scope, not a filesystem sandbox. It requires no global hook installation; see
[managed worker profiles](docs/agent-sessions/source/pages/guides/profiles.md#run-a-managed-codex-turn).

## Resource admission

The [resource broker](docs/agent-sessions/source/pages/guides/resources.md) provides
host-local FIFO tickets for native supervisors. Waiting, admission and release
are machine-consumed state; peer messages remain for decisions and handoffs.
The broker preserves unresolved native outcomes and exposes scoped terminal
events without starting model turns. It adds no background service or MCP server.
Timestamped tickets expose queue position and estimated wait. Resumable
checkpoints survive compaction; the packaged Python client and
[example adapter](examples/shared_resource.py) show the supervising lifecycle.

## Documentation

Read the guides at [cavi-ai.xyz/docs/agent-sessions](https://cavi-ai.xyz/docs/agent-sessions).
The source pages are linked below.

Start with the [overview](docs/agent-sessions/source/pages/introduction/overview.md)
or follow the [complete quickstart](docs/agent-sessions/source/pages/introduction/quickstart.md).

- [Messaging and claims](docs/agent-sessions/source/pages/guides/messaging.md):
  recipients, delivery, ownership and replies.
- [Partner sessions](docs/agent-sessions/source/pages/guides/partners.md):
  create or attach Codex and Claude partners, including multiple partners.
- [Worker profiles](docs/agent-sessions/source/pages/guides/profiles.md):
  pin configuration, launch a worker and resume the same session.
- [Troubleshooting](docs/agent-sessions/source/pages/guides/troubleshooting.md):
  resolve missing messages, launch failures and profile mismatches.
- [CLI reference](docs/agent-sessions/source/pages/reference/cli.md) and
  [configuration](docs/agent-sessions/source/pages/reference/configuration.md).
- [Security model](docs/agent-sessions/source/pages/security/model.md) and
  [version and support](docs/agent-sessions/source/pages/release/version-and-support.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup and checks.

## Security

Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## License

MIT. See [LICENSE](LICENSE).
