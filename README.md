# Skeleton Crew

Self-hosted orchestrator that lets coding agents resolve GitHub issues under guardrails enforced in code.

When you incorporate skeleton crew into your project, then the skeleton crew will start to:

- pick issues labelled `skeleton`, and for each one starts a coding-agent worker in its own container, which opens a PR and keeps fixing until CI and the orchestrator review are green.
- refine issues labelled `skeleton refine` into an actionable spec and hands them back to you with the label `skeleton HITL` (Human In The Loop).

The skeleton crew never merges and never adds the labels `skeleton` or `skeleton refine` themselves. The human user is that gate that decides what gets picked up and what gets merged. The full label lifecycle is in [docs/labels.md](docs/labels.md).

## Security

Skeleton crew agents never hold a GitHub token. They only return commits and schema-validated JSON. A deterministic controller checks both against policy, and only then performs the few GitHub actions it is allowed to. Threat model and guardrails are in [SECURITY.md](SECURITY.md).

## Requirements

- A dedicated Ubuntu 24.04 LTS or Debian 12 VM with Docker and the [Sysbox](https://github.com/nestybox/sysbox) runtime.
- A custom GitHub App installed on the target repository.
- Credentials for the coding agent of choice.

## Install

Step-by-step guide for the VM, GitHub App, ruleset and first run: [docs/install.md](docs/install.md).

## License

[Apache-2.0](LICENSE)