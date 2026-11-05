# Configuration

## Environment variables

| Variable | Default | Purpose |
| --- | --- | --- |
| `CODEX_HOME` | `~/.codex` | Codex session database, rollouts and hook settings |
| `CLAUDE_CONFIG_DIR` | `~/.claude` | Claude transcripts and hook settings |
| `AGENT_SESSIONS_DATA` | `~/.local/share/agent-sessions` | Queues, receipts, claims, pairs, profiles and bindings |
| `CODEX_THREAD_ID` | Supplied by Codex | Native caller identity |
| `CLAUDE_CODE_SESSION_ID` | Supplied by Claude Code | Native caller identity |
| `CLAUDE_PID` | Supplied by Claude Code | Resolve caller identity when native hosts are nested |
| `AGENT_SESSIONS_PROFILE_ID` | Set by the guarded launcher | Identify the pinned launch configuration |

Both hosts must use the same message directory. Keep it outside source control.
Use native caller variables as supplied by the host; do not manufacture an ID from
a directory. Commands that accept `--session ID` allow an explicit selection.
The profile marker is launcher metadata, not an account credential.

## Discovery sources

| Source | What is read |
| --- | --- |
| Codex | `threads` in the highest-numbered `state_<n>.sqlite`, opened read-only, and the tail of each thread's rollout |
| Claude Code | `$CLAUDE_CONFIG_DIR/projects/*/<session>.jsonl`, active by modification time |
| Git | Current checkout, common Git directory, branch and uncommitted paths |

Git discovery disables optional locks and filesystem-monitor hooks. Host database
and transcript formats are implementation details and may change. Activity is
inferred from records, not process liveness. Codex approval-review threads are
hidden unless requested with `list --reviews`.

## Native hook files

`configure codex` writes `$CODEX_HOME/hooks.json`. `configure claude` writes
`$CLAUDE_CONFIG_DIR/settings.json`. It merges handlers for `SessionStart`,
`UserPromptSubmit`, `PreToolUse` and `PostToolUse` and preserves unrelated settings.

Commands include absolute interpreter and module paths plus the receiving host.
Changing the installation path can leave obsolete handlers; see
[Hook setup](../guides/hooks.md) before moving or removing an install.

## Stored state

New message and state files use owner-only permissions. Contents are plaintext
and remain until removed by the user.

| Location under the data directory | Purpose |
| --- | --- |
| `queue/<key>/new/` | Messages waiting to be received or claimed |
| `queue/<key>/cur/` | Session messages already received and still claimable |
| `receipts/<message-id>/` | Per-session receipts for directory messages |
| `claimed/` | Claimed messages and received replies |
| `pairs/` | Partner endpoints and pending creation or launch requests |
| `profiles/` | Pinned configuration identities and hashes |
| `bindings/` | Native session profile checks |
| `state/` | Session startup and peer-warning state; legacy delivery history |

Queue keys route messages to a session or directory. Use `inbox`, `claim` and
`status` instead of moving message files manually. A directory message remains
available to later sessions until claimed.

Earlier JSONL inboxes migrate on first receipt. Migration preserves legacy delivery
records and stops before publishing messages if a state file is unreadable. Do not
delete delivery or profile metadata while retaining work that depends on it.

See [Troubleshooting](../guides/troubleshooting.md) and the
[security model](../security/model.md).
