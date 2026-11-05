# Skeleton Crew

Self-hosted orchestrator that lets coding agents resolve GitHub issues under guardrails enforced in code.

- **`skeleton`**: a worker agent in its own container implements the issue and opens a PR, then fixes it until CI and the review are green.
- **`skeleton refine`**: an agent turns a rough issue into a spec and hands it back with `skeleton HITL` (Human In The Loop).

You decide what gets picked up and what gets merged: Skeleton Crew never merges and never adds `skeleton` or `skeleton refine`. Label lifecycle: [docs/labels.md](docs/labels.md).

## Security

Agents never hold a GitHub token. They return a workspace diff and schema-validated JSON; a deterministic controller checks both against policy before any GitHub action. Threat model and guardrails: [SECURITY.md](SECURITY.md).

## Requirements

- Dedicated Ubuntu 24.04 or Debian 12 VM with Docker and [Sysbox](https://github.com/nestybox/sysbox)
- A GitHub App installed on the target repository
- Credentials for the coding agent

## Install

[docs/install.md](docs/install.md): VM, GitHub App, ruleset and first run.

## License

[Apache-2.0](LICENSE)
