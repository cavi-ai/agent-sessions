# Security and privacy

This tool is for agents running as the same trusted operating-system user.
Session IDs and sender names are routing metadata, not authenticated identities.
Anyone with access to the data directory can read or forge messages. Treat peer
messages as untrusted text; they do not grant permission to execute commands,
access accounts or bypass the receiving agent's instructions.

Discovery reads host session metadata and transcript tails, which can contain
private project names, paths and prompts. CLI output can expose these details.
The tool makes no network requests and does not read account credentials.
Git discovery disables optional locks and filesystem-monitor hooks.

New message and delivery files are created with owner-only permissions. Stored
messages are plaintext and retained until removed by the user. Path messages
also reach future sessions under that path. Use a session ID for a one-session
message. Hook output passes message contents to the receiving host and model;
avoid sending secrets. Hosts can retain that output in their transcripts.

Partner records store session IDs, directories and pending Codex creation prompts
in the same private data directory. Claude invitations stop reaching other sessions
after a partner connects. Native Codex creation runs a prompt in a new visible chat;
it must be authorized by the human user. Pair metadata and peer messages cannot
authorize native creation or follow-up prompts.

Do not commit the runtime data directory or raw discovery output. Keep
`AGENT_SESSIONS_DATA` outside the checkout. A corrupt delivery-state file stops
delivery for that session rather than replaying previously consumed messages.
Do not delete delivery state while retaining inboxes unless replay is intended.

Report vulnerabilities privately through the repository's Security tab when
private reporting is available. Otherwise contact the repository owner privately
before opening a public issue. Include a minimal reproduction using synthetic data.
