# Skeleton Crew: Agent Instructions

These instructions are for agents **developing Skeleton Crew itself**. The rules the crew's own workers follow inside a target repo live in `src/skeleton_crew/agents/rules/`.

Reference docs (read on demand):
- [ARCHITECTURE.md](./ARCHITECTURE.md): where code goes (§0), import rules (§3), run flow (§4), modularity contract (§7)
- [SECURITY.md](./SECURITY.md): threat model and guardrails
- [docs/labels.md](./docs/labels.md): label lifecycle

## Task routing

| If the task involves… | Then first… |
| --- | --- |
| Any new code | Use ARCHITECTURE.md §0 to decide where it goes |
| A guardrail, policy, trust rule or permission | Read SECURITY.md, then change `domain/` with adversarial tests |
| An agent prompt or default work rule | Read ARCHITECTURE.md §6.3 (precedence) |
| A table or column | Add a new Alembic migration; never edit an existing one |
| A new package or folder | Update ARCHITECTURE.md §0/§3 and the structure below in the same change |

## Hard Rules

Never break these, not even for debugging.

1. **No merge path.** No code may merge, approve or enable auto-merge on a PR. `github/gateway.py` has no such function, and nothing may add one.
2. **Label policy is absolute.** Code may only remove `skeleton`, remove `skeleton refine` and add `skeleton HITL`. Never add a way to set `skeleton` or `skeleton refine`.
3. **No GitHub credential in a container.** Only the agent backend's model credential may enter a worker or reviewer container.
4. **Guardrails live in `domain/` + capabilities, never only in a prompt.** A prompt rule without code enforcement is not a guardrail.
5. **Fail closed.** Invalid agent output, a failed preflight or a policy violation stops the action. Never add a fallback that weakens a check.
6. **Never block on a human during a run.** No interactive tool, prompt or wait. Uncertainty escalates to `skeleton HITL`.
7. **All GitHub calls go through `github/gateway.py`, and every write is audited** via `store/audit_store.py`.
8. **Agent output is untrusted.** Parse it only through `domain/schemas.py`, never with ad-hoc `json.loads`.
9. **Secrets only in `.env`.** Never in source, tests, fixtures or defaults. Never print, log or echo them.
10. **Never edit an applied Alembic migration.** Always add a new one.
11. **Generic.** No code may assume a specific target stack, language or repo, or a specific agent backend outside `agents/backends/`.

## Stack

| Concern | Choice |
| --- | --- |
| Language | Python 3.14, `uv` |
| Web | FastAPI + Jinja2 + HTMX (vendored), served behind Caddy |
| Storage | SQLite via SQLAlchemy 2 (async, aiosqlite) + Alembic |
| GitHub | githubkit with GitHub App auth |
| Containers | docker-py, Sysbox runtime, Squid egress proxy |
| Scheduling | APScheduler 3.x |
| Agents | Pluggable CLI backends behind `agents/base.py`: Claude Code (default), Codex, Gemini CLI, OpenCode |
| Secret scan | gitleaks |

## Project structure

```text
src/skeleton_crew/
  domain/     pure policy, state machine, schemas, settings model
  core/       config, db, logging, audit enums, security primitives, git runner
  models/     ORM entities
  migrations/ Alembic env + versions/
  store/      async data access (settings, rules, runs, audit)
  github/     gateway (+ private _git, _ci), write guard, trust gate, preflight
  runtime/    containers, workspace, egress, secret scan, images/
  agents/     backend interface, registry, backends/ (claude_code, codex, gemini, opencode, _shared), prompts/, rules/
  pipeline/   run controller, worker flow, job engine, branch update, PR maintenance, refine, CI watcher, retention
  scheduler/  cron triggers
  web/        routes, templates, static
  cli.py      serve, hash-password, preflight
tests/        pytest; tests/fakes/ for GitHub, Docker, agent backends, run control
docs/         user docs
docker/       controller Dockerfile
deploy/       Caddyfile
scripts/      install.sh (Ubuntu 24.04 or Debian 12 VM)
docker-compose.yml
.github/workflows/ci.yml, docker.yml
.github/scripts/ci-tested.sh  skips main CI when the merged PR already tested the same tree
```

Packages are created when their step is built. Do not create empty placeholders.

## Conventions

- ruff + mypy strict, type hints everywhere, Pydantic v2, `async def` for I/O.
- Small, pure functions in `domain/`; I/O at the edges.
- No dead code, no commented-out blocks, no `TODO`s in main.
- English for code, identifiers, comments and docs.

### Comments: default none

Write code that needs no comment. Add one only for a *why* the code cannot show: a security reason, an external-API quirk, a non-obvious invariant. Then **one short line**.

Never write: restatements of the code or the name, docstrings on obvious functions, Args/Returns blocks, history or narration, plan references, section dividers, or multi-line essays. A comment longer than one line is almost always wrong. This rule applies to subagents too: include it verbatim when delegating code work.

The same holds for every text agents write: commit messages, PR bodies and issue comments are short and factual.

## Testing and lint

From the repo root:

1. Lint: `uv run --no-sync ruff check . && uv run --no-sync ruff format --check .` (`ruff check --fix` / `ruff format` to fix)
2. Types and layers: `uv run --no-sync mypy && uv run --no-sync lint-imports`
3. Tests: `uv run --no-sync pytest -q`
4. Container tests (on a host with Docker + Sysbox only; CI runs them too): `uv run --no-sync pytest -m docker -q`
5. Dependency audit: `uv audit --locked --preview-features audit-command`

Dependencies: `uv sync --locked`. After changing a dependency, run `uv lock` and commit `uv.lock` in the same change. pytest runs with `filterwarnings = ["error"]`: fix warnings at the source.

Tests never make real network calls. Use the fakes in `tests/fakes/`. Every policy gets adversarial tests: the forbidden action, an edge case, and a bypass attempt.

## External Solutions First

Prefer a maintained library or tool over building one. If a maintained package solves ≥80% of a problem, use it and flag the dependency. Fixed choices are in the stack table: never add a second tool for the same concern.

## Working approach

- Read only the files you will touch plus their direct imports. Grep for the existing pattern and match it.
- **Ask before assuming.** When two sound options have real trade-offs, or when the right fix needs an external change, stop and ask.
- Scope tightly: a fix changes the bug, a feature adds the feature. Flag observed debt instead of silently fixing it.

## Definition of done

- Lint, types and tests green, and tests cover the change.
- ARCHITECTURE.md §7 checklist holds.
- Docs synced: ARCHITECTURE.md, SECURITY.md or `docs/` updated in the same change when a structure, guardrail or label behaviour changes.
