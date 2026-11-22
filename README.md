# Agent Sessions

Run a team of specialized agents from one focused main session.

![Five raccoon specialists carry small kits past a luggage cart piled high](assets/agent-sessions-hero.png)

[![Release](https://img.shields.io/github/v/release/cavi-ai/agent-sessions)](https://github.com/cavi-ai/agent-sessions/releases/latest)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

Give each worker a focused brief, a pinned toolset, and its own session.
Queue handoffs and bring the results back when you need them.

[Get started](#get-started) · [Worker profiles](docs/agent-sessions/source/pages/guides/profiles.md) · [Integration guides](#use-your-existing-harness)

## A leaner start for your team

Start workers with the tools their assignments need. Keep those sessions
available for follow-up work.

<details>
<summary>Measured startup example</summary>

One regular startup used **35,057 input tokens**. One startup with the scoped
Agent Sessions profile used **17,265**, a **51% reduction**. Both used the same
CLI, model, working directory, and one-word prompt. The regular launch used
xhigh reasoning effort and the scoped launch used the native default. Counts
include cached input.

The regular profile enabled five MCP servers. The scoped profile excluded
optional MCP servers and automatic skill and memory context, and kept mandatory
host and project instructions. See the [source snapshot used for this example](https://github.com/cavi-ai/agent-sessions/tree/362793c988f044d452359260c94aa51fa141a8df).

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

Install the Agent Sessions plugin from the Cavi marketplace for Claude Code or Codex to make its coordination skill available. The plugin requires the CLI above; it does not install hooks. See [plugin installation](docs/agent-sessions/source/pages/introduction/installation.md#install-the-plugin) for commands and optional hook setup.

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

## Why starting-context profiles matter (5-agent fanout benchmark)

When you fan out parallel workers, every worker inherits your global tool
schemas, plugins, and MCP servers by default. That startup overhead compounds
across every worker and turn, accelerating context rot and crowding out the
actual task—especially on smaller-context local models and high-concurrency
fanouts.

Measured on a live 5-agent concurrent security-audit fanout in Antigravity
(`agy`, `Gemini 3.8 Flash (Low)`):

| Profile tier | Input tokens | Total tokens | Input savings | Wall time | Accuracy |
| --- | ---: | ---: | ---: | ---: | :---: |
| Unprofiled baseline (inline) | 111,452 | 112,961 | — | 22.50s | 5/5 |
| Profiled tool-free (`notools`) | 3,775 | 7,389 | **96.6%** | 14.70s (1.53x faster) | 5/5 |
| Unprofiled with tools (2-turn) | 228,667 | 231,387 | — | 27.64s | 5/5 |
| Profiled scoped-tools + guarded hooks (`scoped-tools`) | 39,849 | 43,971 | **82.6%** | 23.43s | 5/5 |

Pinning a minimal starting context cuts fanout input tokens by **96.6%** for
tool-free workers and **82.6%** for scoped-tools workers while keeping live
hook guards enforced. See [Worker profiles](docs/agent-sessions/source/pages/guides/profiles.md)
for profile tiers, scoped-MCP results, and setup examples.

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
