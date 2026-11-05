# Version and support

This documentation covers Agent Sessions {{PRODUCT_VERSION}}.
The package version is declared in `pyproject.toml`.

## Requirements and compatibility

| Component | Support |
| --- | --- |
| Python | 3.9 or newer; 3.9 and 3.10 install `tomli` for TOML parsing |
| Operating system | macOS or Linux; POSIX file locking is required |
| Git | Installed and available on `PATH` for repository inspection |
| Codex and Claude Code | Installed separately; discovery depends on their local record formats |
| Codex app partner creation | Requires saved local projects and the native app tools |
| Worker profiles | Require supplied native configuration plus loaded, trusted hooks |

Missing hosts do not prevent using the other host. Discovery shows recent records
and cannot establish process liveness. Database, transcript and hook interfaces
can change with native host updates.

## Verification scope

The automated suite uses temporary Git repositories, queues and synthetic native
host records. It checks routing, claims, migration, launch arguments and profile
guards. Those tests do not prove that a user's host has loaded or trusted its hooks,
or that a profile describes the host's full tool catalog.

Verify setup with the [quickstart](../introduction/quickstart.md) and inspect the
native hook definitions before relying on automatic delivery or bound profiles.

## Getting help

For a bug report, include the CLI command, relevant host and Python versions, and
a minimal reproduction with synthetic data. Remove session titles, personal paths,
message contents and account information that are not needed to explain the issue.

Use [GitHub issues](https://github.com/cavi-ai/agent-sessions/issues) for ordinary
bugs. Report vulnerabilities privately through the
[security policy](https://github.com/cavi-ai/agent-sessions/blob/feat/sessions/SECURITY.md).

## License

MIT. See [LICENSE](https://github.com/cavi-ai/agent-sessions/blob/feat/sessions/LICENSE).
