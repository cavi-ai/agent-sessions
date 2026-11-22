# Agent Sessions

Run a team of specialized agents from one focused main session.

![Five raccoon specialists travel light. The scoped-tools benchmark used 82.6% fewer input tokens across five workers.](assets/agent-sessions-hero.png)

[![Release](https://img.shields.io/github/v/release/cavi-ai/agent-sessions)](https://github.com/cavi-ai/agent-sessions/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Give each worker a focused brief, a pinned toolset, and its own session.
Queue handoffs and bring the results back when you need them.

[Get started](#get-started) · [Worker profiles](docs/agent-sessions/source/pages/guides/profiles.md) · [Integration guides](#use-your-existing-harness)

## Five-worker benchmark

Five concurrent security-audit workers ran on Antigravity using Gemini 3.8 Flash (Low).

| Agent Sessions profile | Unprofiled input | Profiled input | Fewer input tokens |
| --- | ---: | ---: | ---: |
| Scoped tools with guarded hooks | 228,667 | 39,849 | **82.6%** |
| Tool-free | 111,452 | 3,775 | **96.6%** |

Both profiles scored **5/5** for accuracy. The scoped-tools run verified all five live hook bindings.
See [the full results and worker profiles](docs/agent-sessions/source/pages/guides/profiles.md#measured-5-agent-fanout-benchmarks).

## Build the workflow around the work

- **Give specialists a stable scope.** Pin a worker's directory, native settings
  and selected configuration files. Guarded launch and resume check those files
  before continuing. Managed Codex runs also check the effective MCP tool inventory
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
uv tool install git+https://github.com/cavi-ai/agent-sessions.git@v0.3.0
```

To work from an editable checkout, run:

```sh
uv tool install --editable --reinstall .
```

See [installation](docs/agent-sessions/source/pages/introduction/installation.md)
for the corresponding plugin and hook setup.

Give your coordinator the [worker-profile guide](docs/agent-sessions/source/pages/guides/profiles.md)
and a bounded assignment:

> Prepare specialists for research, implementation and review. Give each only
> the brief and tools its stage needs. Queue the handoffs, collect one useful
> result per stage, and keep detailed execution in the worker sessions.

Find an existing peer before starting shared-project work or expensive tests:

```sh
agent-sessions who . --host codex --match 'integration'
agent-sessions send SESSION_ID 'Are you running the integration suite? I can wait or take a different shard.'
```

Copy the full ID from the display or JSON. Delivery waits for a host hook or
`inbox`. It does not start a session. Use the [partner guide](docs/agent-sessions/source/pages/guides/partners.md)
to connect existing workers or prepare new ones. For shared native resources,
integrate the [resource client](docs/agent-sessions/source/pages/guides/resources.md)
with your supervising harness.

[Installation and upgrades](docs/agent-sessions/source/pages/introduction/installation.md) ·
[Complete quickstart](docs/agent-sessions/source/pages/introduction/quickstart.md)

## Use your existing harness

Agent Sessions provides a local CLI and a packaged Python resource client.
Keep your coordinator and native agent tools; use the session records, profiles
and queues where your workflow needs them.

| Surface | Source checkout support |
| --- | --- |
| Native sessions | Codex, Claude Code, and Antigravity (`agy`) discovery, messaging and partner records |
| Profiled workers | Pinned configuration with guarded launch and resume for all three hosts |
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

[Full documentation](https://github.com/cavi-ai/agent-sessions/tree/main/docs/agent-sessions/source/pages) ·
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
