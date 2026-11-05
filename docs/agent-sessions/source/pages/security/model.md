# Security model

Agent Sessions coordinates native hosts running as the same trusted operating-system
user. Session IDs, sender names, pair records and profile markers are routing
metadata. They are not authenticated identities or authorization to perform work.

## Message and account boundaries

Treat messages as untrusted peer context. A message or claim does not grant
permission to edit a repository, execute commands, access accounts or override the
receiving session's instructions.

Discovery and routing make no network requests and do not read account credentials.
A profile launch starts the native host with its normal runtime and permissions.
The host may send hook context to its model. The CLI does not sign in to accounts
or provide credentials for the native host.

Native Codex chat creation and follow-up prompts are visible actions that require
the human user's authorization. Another session's invitation does not supply it.

## Local data and privacy

CLI output can expose session titles, prompts, local paths and edited files.
Queues, pair briefs and pending native creation prompts also contain private text.
Keep the data directory outside source control and avoid sending secrets.

New storage uses owner-only permissions, but remains plaintext. Anyone who can
access it as the same user can read or forge messages. The receiving host can
retain hook output in its own transcript.

## Receipts and claims

A receipt records that the tool emitted a message, not that a model accepted it.
A host failure after receipt can lose the associated hook output; the message
remains claimable. Claims record ownership of a message, not a security lease on
files or an account.

Preserve queue and delivery history when repairing storage. Removing records can
cause replay or make ownership impossible to recover from the remaining data.

## Profile checks

Profiles pin explicitly supplied native configuration files and a directory.
Loaded, trusted hooks check the launch marker and hashes before prompts and
supported tools. Bare reopening does not silently remove an existing binding.

This check is not a complete sandbox or proof of the effective tool catalog.
Native managed settings and tool paths outside hook coverage can affect behavior.
Keep native permissions and project instructions in force. Do not bypass hook
trust to activate a profile.

Codex capability overrides are visible in process arguments and dry-run output.
Keep secrets out of those settings, URLs and command arguments. Inline MCP
environment values, static HTTP headers and bearer tokens are rejected. Use native
environment-variable references instead; their values are not expanded into argv.

## Report a vulnerability

Follow the private reporting instructions in
[SECURITY.md](https://github.com/cavi-ai/agent-sessions/blob/feat/sessions/SECURITY.md).
Use synthetic data for reproductions. Do not post actual queues, host transcripts,
credentials or personal paths in a public issue.
