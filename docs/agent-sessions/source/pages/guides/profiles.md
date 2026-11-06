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

## Run a managed Codex turn

```sh
agent-sessions partner run PAIR_ID --timeout 60
agent-sessions partner run PAIR_ID --prompt-file /path/to/prompt.txt --timeout 60
agent-sessions partner show PAIR_ID
```

`partner run` owns one App Server process and checks the profile directly, without
command hooks. The pinned Codex profile must disable `features.hooks`,
`features.apps`, `features.plugins`, `features.skill_search` and
`skills.include_instructions`. Every enabled MCP server must declare `enabled_tools`.
Unexpected inherited servers or discovered tools prevent assignment. Model and
permission settings retain their native configuration behavior.

Required servers may report `notStarted` or `starting` while their initial
connection is pending. The runner waits within the existing run deadline, then
requires their exact connected tool inventory before submitting work. A failed,
cancelled, disabled or authentication-required server stops assignment. Startup
waiting does not restart a server or initiate a login.

Use `--no-mcp` when the caller requires an empty MCP scope. It rejects any enabled
server in the pinned profile before starting an App Server process. Effective
configuration and discovered-tool checks still apply to profiles that pass.

The default prompt is the saved pair brief. `--prompt-file` supplies later work
without changing that brief. It requires a canonical absolute path to a regular
UTF-8 file, at most 64 KiB. The JSON receipt contains `pair_id`, `session_id`,
`profile_id`, `turn_id`, `status`, `output_text`, `prompt_sha256` and `capabilities`.
The capabilities contain checked `mcp_tools` names and `automatic_skill_context:
false`, which records the required context settings, not a filesystem restriction
or native asset acceptance. Output text is limited to 64 KiB.

Successful later runs resume the same session and reject active or unexpected
latest turns. `partner show` exposes the saved `last_run` receipt. `completed`
means the native turn completed; `failed` means it ended unsuccessfully;
`attention` means a prerequisite prevented assignment; `unknown` means the native
outcome needs reconciliation. Only `completed` exits successfully. Timeout
interrupts only the turn owned by that run and closes its transport. SIGTERM and
SIGINT use the same cancellation path. Failed or
unknown runs are retained and are not replayed or replaced by later runs.

Fresh idle threads must return explicitly empty history and still be idle with
empty history after MCP startup. External steering during startup prevents the
first assignment. An `attention` receipt alone does not prove the session is idle
or release a consumer's native reservations.
Legacy persistent roots can report the exact native unmaterialized-history error
before their first user message. Only that response, after a current identity,
directory and idle-state check, counts as absent history; other read failures stop
assignment.
If a preassignment `attention` session later disappears, two
exact native absence responses are required before replacing it. The pair retains
the prior ID, receipt and absence evidence. Submitted or uncertain runs cannot use
this reconciliation. Native error codes and bounded messages appear in `last_run`.

When invoked from a native session, `partner run` requires that session to be one
of the pair's endpoints. Terminal use without a native session identity remains
available.

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

Managed receipts may include up to 64 host-observed MCP call records. Each contains
the call ID, server, tool, terminal status and SHA-256 digests of the arguments and
structured result. Raw arguments and results are omitted. Calls must belong to the
owned turn and pinned tool scope; duplicate completion IDs must agree. These
records establish what the native host observed, not downstream artifact quality.
