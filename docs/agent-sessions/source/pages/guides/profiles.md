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
| Antigravity (`agy` / `antigravity`) | `.agents/settings.json` and `.agents/mcp_config.json` (optional `.agents/agents/<name>/agent.md` or `.agents/agents/<name>.md`) |

Pin the directory, using a short profile name:

```sh
agent-sessions profile create codex /path/to/worker modeler
agent-sessions profile create claude /path/to/worker reviewer
agent-sessions profile create agy /path/to/worker auditor
```

For Antigravity, if `.agents/agents/<name>/agent.md` or `.agents/agents/<name>.md`
matches the profile name, `profile create agy` pins it automatically (or pass it
explicitly with `--file`). Each `agy` profile supports at most one custom agent
file; its YAML frontmatter `name` must match the file path and cannot set
`mainAgent: false`. The `antigravity` host alias is accepted anywhere `agy` is.

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

Use `partner claude` for a Claude profile or `partner agy` for an Antigravity
profile. Profiled partners return a guarded
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

For Antigravity (`agy`) profiles, `profile launch` and `partner resume` also
support non-interactive headless execution with `--print 'PROMPT'`, `--headless`,
and `--output-format text|json|stream-json` (not combinable with initial `--pair`
bootstrap):

```sh
agent-sessions profile launch PROFILE_ID --print 'Audit src/auth.py' --output-format json
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
agent-sessions partner run PAIR_ID --complete --archive --metrics
agent-sessions partner show PAIR_ID
```

`partner run` owns one App Server process and checks the profile directly, without
command hooks. `--complete` records the finished pair, `--archive` requests native
Codex archival after completion, and `--metrics` includes native turn metrics when
available. The pinned Codex profile must disable `features.hooks`,
`features.apps`, `features.plugins`, `features.skill_search`, `features.memories` and
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
The capabilities contain checked `mcp_tools` names, `automatic_skill_context:
false` and `memory_context: false`. Context fields remain null until the effective
settings pass their checks. They record configuration checks, not inspection of
the model's full prompt, a filesystem restriction or native asset acceptance.
Output text is limited to 64 KiB.

Fresh workers start as persistent roots without coordinator history. Supply the
selected guides and requirements in the bounded prompt rather than loading the
whole reference library. Mandatory host and applicable project instructions remain
in force. When changing context controls, audit actual native turn context for
injected memory summaries, skill catalog entries and unrelated plugin guidance;
`skills/list` inventory and a config hash alone cannot establish prompt isolation.

Successful later runs resume the same session and reject active or unexpected
latest turns. `partner show` exposes the saved `last_run` receipt. `completed`
means the native turn completed; `failed` means it ended unsuccessfully;
`attention` means a prerequisite prevented assignment; `unknown` means the native
outcome needs reconciliation. Only `completed` exits successfully. Timeout
interrupts only the turn owned by that run and closes its transport. SIGTERM and
SIGINT use the same cancellation path. Failed or
unknown runs are retained and are not replayed or replaced by later runs.

Resume also requires the previous receipt's checked `memory_context: false`.
Disabling memory after a turn does not remove a summary already in its history.
Older workers without that evidence stop before launch/resume. Preserve their
identities and reconcile owned work; consumers can retire the old assignment
and prepare a fresh scoped worker without relabeling the old history as clean.

Fresh idle threads must return explicitly empty history and still be idle with
empty history after MCP startup. External steering during startup prevents the
first assignment. An `attention` receipt alone does not prove the session is idle
or release a consumer's native reservations.
If full-history reads report the specific unsupported `list_turns` response, the
runner requires a successful empty `thread/turns/list` result instead. A history
error is never treated as an empty history. Metadata existence checks do not
require full-history support.
If both reads report that specific unsupported response for a newly created
root, the runner closes its transport and reloads the same bound identity once.
It repeats configuration, profile, identity, idle-state and MCP checks within the
original deadline, then requires a valid empty turn page before assignment. It
does not replace the session or submit work to materialize its history. Other
history failures stop the run.
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

## Use a local provider

For a managed turn, `--local-provider` reads the model and selected provider from
the pinned `.codex/config.toml`. The provider must use a literal loopback HTTP
address (`127.0.0.1` or `[::1]` with an explicit port), the Responses API and a
nonempty key supplied by environment variable. The pinned configuration must
select exactly one custom provider. Its name must be a simple custom name other
than `openai`, `ollama` or `lmstudio`:

```toml
model = "your-local-model"
model_provider = "local"

[model_providers.local]
name = "Local"
base_url = "http://127.0.0.1:8095/v1"
env_key = "LOCAL_GATEWAY_KEY"
wire_api = "responses"
requires_openai_auth = false
supports_websockets = false
```

Supply `LOCAL_GATEWAY_KEY` only in the managed runner's environment, then run:

```sh
agent-sessions partner run PAIR_ID --local-provider
```

Managed local mode disables reasoning summaries and hosted web search. Account
authentication and WebSockets must be disabled. Both default to false. An optional
`stream_max_retries` integer from 0 to 5 pins the provider stream retry limit.
The runner checks the effective model and provider before assignment. Resume with
the same option and pinned provider. The key value is not included in the profile
or launch arguments.

## Starting-context profile tiers (Antigravity)

Controlling what a worker inherits at startup lets you match context size to the
task:

- **Tool-free / zero-MCP**: Pin a custom agent with
  `excludeDefaultComponents: true` and `inheritCustomizations: false` alongside
  an empty `.agents/mcp_config.json` (`{"mcpServers": {}}`). Best for pure
  classification, review, or audit workers that read inline input. This can also
  exclude global customizations containing guard hooks. Hook protection applies
  only when the host loads those hooks.
- **Scoped-tools + guarded hooks**: Keep `inheritCustomizations: true` so
  global hooks stay active, set `inheritMcp: false`, and restrict built-in
  capabilities with an explicit `tools: [...]` list.
- **Scoped-MCP**: Declare only the required servers under `mcpServers` in the
  pinned `.agents/mcp_config.json` and grant them in the agent definition via
  `mcpServers: [...]` while keeping guarded hooks active.

## Measured 5-agent fanout benchmarks

Measured in live Antigravity (`agy`) runs using `Gemini 3.8 Flash (Low)` across
5 concurrent security-audit workers:

| Configuration | Input tokens | Total tokens | Savings vs baseline | Wall time | Verification |
| --- | ---: | ---: | --- | ---: | --- |
| Unprofiled baseline (inline) | 111,452 | 112,961 | — | 22.50s | 5/5 accuracy |
| Profiled tool-free (`notools`) | 3,775 | 7,389 | **96.6% input / 93.5% total** | 14.70s (1.53x faster) | 5/5 accuracy |
| Unprofiled with tools (2-turn) | 228,667 | 231,387 | — | 27.64s | 5/5 accuracy |
| Profiled scoped-tools + guarded hooks (`scoped-tools`) | 39,849 | 43,971 | **82.6% input / 81.0% total** | 23.43s | 5/5 accuracy, 5/5 live hook bindings |
| Profiled scoped-MCP + hooks (`scoped-mcp`) | ~10,271 / worker | ~11,504 / worker | **~77.5% input** vs unprofiled with-tools | — | Live scoped MCP calls + hook bindings |

These measurements cover the selected security-audit tasks and Antigravity
configurations.

## What the launcher preserves

All three hosts use their configured model and native permission settings.
Claude launches repeat the settings and MCP file paths with
`--strict-mcp-config`. Codex repeats the pinned `model`,
`model_reasoning_effort`, app, feature, MCP, plugin and skill settings as native
`-c` overrides, so they apply even when Codex does not load the worker
directory's `.codex/config.toml`. When the profile pins both `model` and
`model_provider`, Codex also receives the provider ID and its definition if the
profile defines it. A provider defined only in native user config must be
available to Codex at launch. Antigravity launches pass `--settings` for
`.agents/settings.json`, `--agent` when a custom agent file is pinned,
`--disable-slash-commands` when `disableSlashCommands: true` is set, and
`--conversation` on resume; `.agents/settings.json` must not define `hooks`.
Native launchers carry the same profile marker across start and resume. Both
native launches and managed Codex runs inherit the caller’s environment with
known host session variables removed. Scope credentials to the runner that needs
them.

Capability overrides are visible in process arguments and dry-run output. Keep
secrets out of capability settings, URLs and command arguments. Codex capability
overrides reject nonempty MCP `env`, `http_headers` and `bearer_token` values.
For model providers pinned in the worker profile, use `env_key` or
`env_http_headers` for credentials. Define providers that need inline headers,
query parameters or other auth configuration in native user config and pin only
their provider ID and model in the worker profile.
Use native references:

- `env_vars` for environment variables forwarded to a stdio server.
- `env_http_headers` for headers populated from environment variables.
- `bearer_token_env_var` for a bearer token read from an environment variable.

Reference values are not expanded into launch arguments. See the official
[Codex MCP configuration](https://learn.chatgpt.com/docs/extend/mcp).

## Limits of the check

A profile verifies its supplied files and launch context. Native host launches
select settings and tools through that host. The host controls its effective
tool catalog. Managed Codex runs separately check the effective MCP tool
inventory before assignment, but do not inspect the full prompt or provide a
security sandbox. Native settings and tool paths outside hook coverage can affect
behavior. Keep native permissions and mandatory project instructions in force.

Keep profile and binding data intact. Removing that metadata can lose the record
needed to check or resume a worker. For recovery steps, see
[Troubleshooting](troubleshooting.md) and the [security model](../security/model.md).

Managed receipts may include up to 64 host-observed MCP call records. Each contains
the call ID, server, tool, terminal status and SHA-256 digests of the arguments and
structured result. Raw arguments and results are omitted. Calls must belong to the
owned turn and pinned tool scope; duplicate completion IDs must agree. These
records establish what the native host observed, not downstream artifact quality.
