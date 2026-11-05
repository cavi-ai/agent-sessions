# Contributing

Use Python 3.9 or newer and Git on macOS or Linux.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
ruff check .
python -m unittest discover -s tests
python -m scripts.docs.build
python -m scripts.docs.verify
python -m build
```

Tests use temporary host databases, transcripts, settings and Git repositories.
Keep fixtures synthetic. Never submit real session logs, messages, databases,
credentials, home-directory paths or local agent configuration. Changes to
discovery should tolerate missing hosts and describe any host-format assumptions.

Before sharing a branch, scan its files and history with
[Gitleaks](https://github.com/gitleaks/gitleaks):

```sh
gitleaks git --redact .
gitleaks dir --redact .
```

Open pull requests with a description of the behavior change and relevant test results.

## Documentation

Edit `docs/agent-sessions/source/pages/` and register pages in `source/navigation.json`.
Keep the README focused on installation, the first message exchange and guide links.
Guides explain tasks; the reference documents command options and configuration.

`scripts.docs.build` produces the versioned product-docs artifact used by the CAVI
documentation site. It stamps the package version and Git identity, then records a
content digest. `scripts.docs.verify` checks that identity and every navigation target.
Relative page links must remain inside the artifact. The documentation tests check
those links, README links, reproducibility and release archive integrity.

Generated `docs/agent-sessions/v*/` output is ignored. Publish the artifact built from
the released commit; website snapshots are not edited by hand.
