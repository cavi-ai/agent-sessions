# Installation

## Requirements

- Python 3.9 or newer and Git.
- macOS or Linux. Windows is not supported because queue storage uses POSIX locks.
- Codex, Claude Code, Antigravity (`agy`), or OpenCode installed separately for the sessions you want to use.

Python 3.9 and 3.10 also need `tomli`; package installation resolves it automatically.
The CLI does not install the hosts or sign in to their accounts.

## Install with uv

Use [uv](https://docs.astral.sh/uv/) to keep the CLI in its own environment:

```sh
uv tool install git+https://github.com/cavi-ai/agent-sessions.git@{{RELEASE_TAG}}
agent-sessions --help
```

The tag above is filled in when documentation is generated for a release. A tag
for an unpublished checkout may not exist yet. Use the editable checkout below
for its features. The earlier `v0.3.0` release does not include OpenCode host
support or the `src/agent_sessions` package layout. The tagged release does not require a PyPI release.

## Install in a virtual environment

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install git+https://github.com/cavi-ai/agent-sessions.git@{{RELEASE_TAG}}
agent-sessions --help
```

Use the installed CLI from the same environment when configuring hooks.

## Install the plugin

The [Cavi marketplace](https://github.com/cavi-ai/plugins/blob/main/catalog.json)
lists Agent Sessions for Claude Code and Codex. Add the marketplace and install
the skill using the commands for your host:

```sh
claude plugin marketplace add cavi-ai/plugins
claude plugin install agent-sessions@cavi-ai
```

```sh
codex plugin marketplace add cavi-ai/plugins
codex plugin add agent-sessions@cavi-ai
```

Marketplace skill revisions follow the catalog. The CLI is installed separately.
The CLI and plugin should come from compatible revisions. Checkout features
require the editable CLI described below.

Use the command for your host; the CLI must be available on its `PATH`. The plugin does not install hooks. For automatic message delivery, review the [hook guide](../guides/hooks.md) and run the host-specific `configure` command yourself. Codex requires its native hook trust review.

## Work from a checkout

From the checkout containing the features you want to use:

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

You can also run `PYTHONPATH=src python3 -m agent_sessions` from the checkout once its dependencies
are installed. If you need a checkout, clone `https://github.com/cavi-ai/agent-sessions.git`
and select the desired revision before installing. See [Contributing](https://github.com/cavi-ai/agent-sessions/blob/main/CONTRIBUTING.md)
for development checks.

## Update or remove

For an installation managed by uv, reinstall with the desired tag or ref to select
a version. The following tag is filled in for generated release documentation.
For an unpublished checkout, use the editable install above:

```sh
uv tool install --reinstall git+https://github.com/cavi-ai/agent-sessions.git@{{RELEASE_TAG}}
```

Remove old hooks before replacing an installation, then run `configure` from the
new CLI. The [hook guide](../guides/hooks.md) explains how handlers are matched
and replaced.

Before uninstalling, remove this installation's hooks:

```sh
agent-sessions configure codex --remove
agent-sessions configure claude --remove
```

For installations with Antigravity or OpenCode hooks, also run
`agent-sessions configure agy --remove` and `agent-sessions configure opencode --remove`
before uninstalling. Then remove the CLI:

```sh
uv tool uninstall agent-sessions
```

Uninstalling the package does not remove queues, pair records or profile bindings.
Keep that data if you intend to resume the same workers.

Continue with the [quickstart](quickstart.md).
