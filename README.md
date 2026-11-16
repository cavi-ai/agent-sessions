# Agent Sessions

[![Release](https://img.shields.io/github/v/release/cavi-ai/agent-sessions)](https://github.com/cavi-ai/agent-sessions/releases/latest)
[![Release asset downloads, all time](https://img.shields.io/github/downloads/cavi-ai/agent-sessions/total?label=release%20assets%20total)](https://github.com/cavi-ai/agent-sessions/releases)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

![Fan out the work. Not the baggage. Five raccoon specialists travel with compact kits beside an impossibly overloaded luggage cart. Five-start projection: 175,285 to 86,325 startup input tokens.](assets/agent-sessions-hero.png)

Build complex workflows around specialized agents. Give each worker a focused
brief and its relevant tools, coordinate through queued handoffs, and bring
useful results back to the main session.

[Get started](#get-started) · [Worker profiles](docs/agent-sessions/source/pages/guides/profiles.md) · [Integration guides](#use-your-existing-harness)

## A leaner start for your team

Copying a broad setup into every worker spends tokens before the work begins.
Agent Sessions lets you prepare separate workers with pinned configuration and
selected tools, then reuse those sessions for later assignments.

| Startup input tokens | Regular startup | Agent Sessions scoped startup |
| --- | ---: | ---: |
| One measured start | 35,057 | 17,265 |
| Five-start projection | 175,285 | 86,325 |

**88,960 fewer startup input tokens in the five-start projection: a 51% reduction
in this reference setup.**

<details>
<summary>Startup measurement</summary>

Measured November 16, 2026, using this [source snapshot](https://github.com/cavi-ai/agent-sessions/tree/362793c988f044d452359260c94aa51fa141a8df) and native CLI 0.166.0.
One startup per configuration used the same model, working directory and
one-word-response prompt, with native startup defaults retained. Both returned
`READY` and made no tool calls.

The regular configuration enabled five MCP servers. The scoped starter profile
disabled optional MCP servers and automatic skill/memory context while retaining
mandatory host and project instructions. Input counts include cached input.
The five-start figures are five times each observed startup input count.

</details>

## Build the workflow around the work

- **Give specialists a stable scope.** Pin a worker's directory, native settings
  and selected configuration files. Guarded launch and resume check those files
  before continuing. Managed runs also verify the effective MCP tool inventory
  before assigning work.
- **Keep the main session focused.** Fresh managed workers start without the
  coordinator's conversation history. Detailed execution lives in the worker;
  the coordinator can collect its result and send a focused follow-up.
- **Make handoffs deliberate.** Durable messages wait in a queue. An atomic claim
  records who took the job and replies to the sender. Sending a message does not
  itself interrupt a session or start a model turn.
- **Queue shared resources.** Participating native supervisors take FIFO tickets,
  inspect queue position and wait estimates, and resume from saved checkpoints.
  Queue bookkeeping runs without model turns; supervisors own execution and
  cleanup.
- **Keep the worker you prepared.** Partner records retain native session
  identities across follow-ups. Managed receipts record the turn, status and
  result, with bounded MCP call hashes when tools are used.

## Get started

Requires Python 3.9+, Git, and macOS or Linux.

```sh
uv tool install git+https://github.com/cavi-ai/agent-sessions.git
```

Give your coordinator the [worker-profile guide](docs/agent-sessions/source/pages/guides/profiles.md)
and a bounded assignment:

> Prepare specialists for research, implementation and review. Give each only
> the brief and tools its stage needs. Queue the handoffs, collect one useful
> result per stage, and keep detailed execution in the worker sessions.

Use the [partner guide](docs/agent-sessions/source/pages/guides/partners.md) to
connect existing workers or prepare new ones. For shared native resources,
integrate the [resource client](docs/agent-sessions/source/pages/guides/resources.md)
with your supervising harness.

[Installation and upgrades](docs/agent-sessions/source/pages/introduction/installation.md) ·
[Complete quickstart](docs/agent-sessions/source/pages/introduction/quickstart.md)

## Use your existing harness

Agent Sessions provides a local CLI and a packaged Python resource client.
Keep your coordinator and native agent tools; use the session records, profiles
and queues where your workflow needs them.

| Surface | Current support |
| --- | --- |
| Native sessions | Codex and Claude Code discovery, messaging and partner records |
| Profiled workers | Pinned configuration with guarded launch and resume for both hosts |
| Managed turns | Codex App Server execution, scope checks and saved run receipts |
| Supervising harnesses | CLI contracts and `resource_client.ResourceClient` for resource admission, lifecycle events and checkpoint/resume |

Optional [host hooks](docs/agent-sessions/source/pages/guides/hooks.md) deliver
queued messages at supported checkpoints. Managed turns check configuration
directly and use their own launch path.

## Access and data

Discovery and messaging run locally. Session titles, project paths and queued
messages stay in local storage until a host passes message context to its model.
Native permission settings remain in force. Worker profiles verify pinned files
and launch context.

[Security model](docs/agent-sessions/source/pages/security/model.md) ·
[Worker configuration](docs/agent-sessions/source/pages/guides/profiles.md)

## Documentation and help

[Full documentation](https://cavi-ai.xyz/docs/agent-sessions) ·
[Overview](docs/agent-sessions/source/pages/introduction/overview.md)

- [Messaging and claims](docs/agent-sessions/source/pages/guides/messaging.md)
- [Partner sessions](docs/agent-sessions/source/pages/guides/partners.md)
- [Worker profiles](docs/agent-sessions/source/pages/guides/profiles.md)
- [Resource queues and Python integration](docs/agent-sessions/source/pages/guides/resources.md)
- [CLI reference](docs/agent-sessions/source/pages/reference/cli.md) and [configuration](docs/agent-sessions/source/pages/reference/configuration.md)
- [Troubleshooting](docs/agent-sessions/source/pages/guides/troubleshooting.md)
- [Version and support](docs/agent-sessions/source/pages/release/version-and-support.md)
- [Report an issue](https://github.com/cavi-ai/agent-sessions/issues) or [contribute](CONTRIBUTING.md)

## License

MIT. See [LICENSE](LICENSE).
