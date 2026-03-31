# Contributing to argus-qa

Thanks for your interest in contributing!

## Setup

```bash
git clone https://github.com/hipjim/argus-qa.git
cd argus-qa
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

You'll also need:
- Python 3.11+
- Node.js 18+ (for Playwright MCP via npx)
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated

## Making changes

1. Fork the repo and create a branch from `main`
2. Make your changes
3. Test manually against a web app (there's no automated test suite yet)
4. Open a pull request

## Guidelines

- Keep PRs focused — one feature or fix per PR
- Follow existing code style
- Update the README if you change CLI flags or behavior
- Prompt changes in `argus_qa/prompts/` are high-impact — test thoroughly

## Reporting bugs

Open an issue with:
- What you ran (command + flags)
- What happened vs what you expected
- Python version, OS, and Node.js version

## Ideas for contributions

- Additional output formats (HTML reports, JUnit XML)
- Better error messages and recovery
- Test plan diffing between runs
- CI/CD integration examples
- Support for more authentication flows
