# Contributing to argus-qa

Thanks for your interest in contributing! Bug reports, docs fixes, and code are all welcome.

## Setup

```bash
git clone https://github.com/hipjim/argus-qa.git
cd argus-qa
python3 -m venv .venv && source .venv/bin/activate
pip install -e '.[dev]'
argus-qa setup
```

You'll also need Node.js 18+ (for Playwright MCP) and [Claude Code](https://docs.anthropic.com/en/docs/claude-code) installed and logged in to run agents. The automated tests don't need either.

[docs/architecture.md](docs/architecture.md) explains how the code is organized.

## Making changes

1. Fork the repo and create a branch from `main`.
2. Make your change, with tests where it makes sense.
3. Run `pytest -q && ruff check .`.
4. If you changed agent behavior or prompts, also try it against a real web app.
5. Open a pull request.

## Guidelines

- Keep PRs focused: one feature or fix per PR.
- Follow the existing code style.
- Update the docs (`README.md`, `docs/`) if you change CLI flags, API endpoints, or behavior.
- Prompt changes in `argus_qa/prompts/` are high-impact. Describe what you tried them on.
- Keep runtime dependencies to a minimum. The server's dependencies belong in the `[server]` extra.

## Reporting bugs

Open an issue with:
- what you ran (command and flags, or the API request),
- what happened and what you expected,
- your Python, Node.js and OS versions.

Please remove passwords and secrets from any logs you paste.

## Ideas for contributions

- CI integration examples (GitHub Actions, GitLab CI)
- Support for more login flows (SSO, 2FA test accounts)
- Comparing results between runs
- Better error messages and recovery
