# Installation

## Requirements

- Python 3.9 or newer and Git.
- macOS or Linux. Windows is not supported because queue storage uses POSIX locks.
- Codex, Claude Code, or both, installed separately for the sessions you want to use.

Python 3.9 and 3.10 also need `tomli`; package installation resolves it automatically.
The CLI does not install the hosts or sign in to their accounts.

## Install with uv

Use [uv](https://docs.astral.sh/uv/) to keep the CLI in its own environment:

```sh
uv tool install git+https://github.com/cavi-ai/agent-sessions.git
agent-sessions --help
```

The package is installed from GitHub. These instructions do not require a PyPI release.

## Install in a virtual environment

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install git+https://github.com/cavi-ai/agent-sessions.git
agent-sessions --help
```

Use the installed CLI from the same environment when configuring hooks.

## Work from a checkout

```sh
git clone https://github.com/cavi-ai/agent-sessions.git
cd agent-sessions
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
```

You can also run `python3 agent_sessions.py` from the checkout once its dependencies
are installed. See [Contributing](https://github.com/cavi-ai/agent-sessions/blob/feat/sessions/CONTRIBUTING.md)
for development checks.

## Update or remove

For an installation managed by uv:

```sh
uv tool upgrade agent-sessions
```

Run `configure` again after updating so the installed hooks match the CLI. If an
update changes the Python or module path, remove the old handlers before replacing
the installation; the [hook guide](../guides/hooks.md) explains the paths involved.

Before uninstalling, remove this installation's hooks:

```sh
agent-sessions configure codex --remove
agent-sessions configure claude --remove
uv tool uninstall agent-sessions
```

Uninstalling the package does not remove queues, pair records or profile bindings.
Keep that data if you intend to resume the same workers.

Continue with the [quickstart](quickstart.md).
