# Contributing

Use Python 3.9 or newer and Git on macOS or Linux.

```sh
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[dev]'
ruff check .
python -m unittest discover -s tests
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
