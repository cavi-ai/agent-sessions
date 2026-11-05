# Worker profiles

Profiles preserve a worker directory and explicitly supplied native configuration
files across launch and resume. They store file identities and hashes, not copies
of the configuration or an inventory of the host's actual tools.

## Prepare and pin native configuration

Put the intended native settings in the worker directory before creating a profile:

| Host | Required files |
| --- | --- |
| Codex | `.codex/config.toml` |
| Claude Code | `.claude/settings.local.json` and `.mcp.json` |

Pin the directory, using a short profile name:

```sh
agent-sessions profile create codex /path/to/worker modeler
agent-sessions profile create claude /path/to/worker reviewer
```

Each command returns a profile ID. To pin additional relative configuration or
skill files, repeat `--file`:

```sh
agent-sessions profile create codex /path/to/worker modeler --file skills/modeling/SKILL.md
```

Pinned files must be regular files within the worker directory, without symlinks.
A profile supports up to 16 files, each at most 2 MiB.

## Create and launch a profiled partner

Use the profile ID returned above:

```sh
agent-sessions partner codex /path/to/worker 'Build the selected mesh' --profile PROFILE_ID
agent-sessions profile launch PROFILE_ID --pair PAIR_ID --dry-run
agent-sessions profile launch PROFILE_ID --pair PAIR_ID
```

Use `partner claude` for a Claude profile. Profiled partners return a guarded
launch command rather than an ordinary app creation request or path invitation.
Run the launch once. The bootstrap connects the session before receiving the work;
the requested task remains in the pair's `brief` until the caller sends it.

The hooks must be installed and trusted, including `PreToolUse`. Connection
requires the matching hook-recorded profile binding.

## Resume the same worker

```sh
agent-sessions partner resume PAIR_ID --dry-run
agent-sessions partner resume PAIR_ID
```

For a session already bound to a profile, the equivalent direct route is:

```sh
agent-sessions profile launch PROFILE_ID --resume SESSION_ID
```

Bare reopening, changed configuration, a wrong directory or damaged bindings block
prompts and supported tools. Resume through the guarded launcher. If a launch's
outcome is uncertain, inspect the native host and pair record instead of starting
another worker.

An existing worker can be attached with `partner attach SESSION_ID --profile
PROFILE_ID` only after the worker has recorded that profile through its launcher
and hooks. Attachment does not apply a profile to an ordinary open chat.

## What the launcher preserves

Both hosts use their configured model and native permission settings. Claude
launches repeat the settings and MCP file paths with `--strict-mcp-config`.
Codex repeats pinned app, feature, MCP, plugin and skill settings as native `-c`
overrides. Both start and resume carry the same profile marker.

Capability overrides are visible in process arguments and dry-run output. Keep
secrets out of capability settings, URLs and command arguments. Nonempty MCP
`env`, `http_headers` and `bearer_token` values are rejected. Use native references:

- `env_vars` for environment variables forwarded to a stdio server.
- `env_http_headers` for headers populated from environment variables.
- `bearer_token_env_var` for a bearer token read from an environment variable.

Reference values are not expanded into launch arguments. See the official
[Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp).

## Limits of the check

A profile verifies its supplied files and launch context. It does not prove the
host's effective tool catalog or provide a security sandbox. Native managed
settings and tool paths outside hook coverage can affect behavior. Keep native
permissions and mandatory project instructions in force.

Keep profile and binding data intact. Removing that metadata can lose the record
needed to check or resume a worker. For recovery steps, see
[Troubleshooting](troubleshooting.md) and the [security model](../security/model.md).
