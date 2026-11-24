# Skeleton Crew: Architecture

For agents: what lives where and which rules hold, so nobody has to scan the code. **Do not read top to bottom**: check §0, then grep for `## <n>.`.
Not here: behaviour, how it works and history. Behaviour lives in code and tests; what a run does is described for people in [docs/run-flow.md](docs/run-flow.md).
One short sentence per fact.

| § | Contents |
| --- | --- |
| 0 | Lookup table: where does X go? (check this first for any new code) |
| 1 | Guiding principles |
| 2 | Topology |
| 3 | Packages and import rules |
| 4 | Run flow: which module owns which stage |
| 5 | Data model |
| 6 | Cross-cutting: config, agent backends, work rules, audit, classifiers, tests |
| 7 | Modularity contract and what enforces it |
| 8 | When to update this document |

---

## 0. Lookup: where does X go?

Each row has exactly one correct place. Putting it anywhere else violates the contract (§7). Paths are relative to `src/skeleton_crew/`.

### Policy (`domain/`, pure)

| You are adding / changing… | It goes in… |
| --- | --- |
| Which label changes are allowed, label comparison | `domain/label_policy.py` |
| Branch naming, the push target, continuing a branch, what GitHub's mergeable flag means | `domain/branch_policy.py` |
| Protected paths, diff size, file-mode rules | `domain/diff_policy.py` |
| Parsing a diff: added lines, raw and numstat summaries, file modes | `domain/diff_parse.py` |
| Conflict-resolution rules (only conflicted files, no conflict markers) | `domain/merge_policy.py` |
| Detecting long code comments in a diff | `domain/comment_policy.py` |
| Findings, objections, contest matching and the registry of contestable checks (`CHECKS`) | `domain/contest_policy.py` |
| Sanitising agent text before it reaches GitHub, text budgets, bytes per character | `domain/text_policy.py` |
| Whether issue content is trusted | `domain/trust_policy.py` |
| What a GitHub write may carry (titles, full SHAs, push targets, new-issue labels) | `domain/write_policy.py` |
| The full commit SHA format | `domain/git_refs.py` |
| Which env and mounts a container may get | `domain/container_policy.py` |
| Ruleset and App-permission rules (`REQUIRED_PERMISSIONS`) | `domain/repo_policy.py` |
| CI evaluation: check states, required checks, job-log formatting | `domain/ci_policy.py` |
| Daily quota maths | `domain/quota_policy.py` |
| Which candidates are excluded, and when an issue goes to PR maintenance | `domain/candidate_policy.py` |
| Worker job decisions: untrusted output, CI state to action, review verdict, deadlines | `domain/job_policy.py` |
| Session rules: writable workspace per role, when a continue may run and its budget | `domain/session_policy.py` |
| Task, refine and run states, transitions, run triggers and steps, round limits (`ROUND_RULES`), job modes | `domain/task_state.py` |
| Task keys, container names, transcript names, the repo-command names | `domain/task_keys.py` |
| The JSON an agent role must return, and its cross-checks | `domain/schemas.py` |
| Which target-repo docs an agent gets and how they are rendered | `domain/repo_docs.py` |
| A UI-editable setting, the rule keys and the rules-content check | `domain/settings.py` |
| Runner names, backoff and slot transitions, runner status and the reasons shown | `domain/runner_policy.py` |
| The classifier questions and what their answers decide | `domain/classifier_policy.py` |
| Domain errors and the `Violation` type | `domain/errors.py` |

### Infrastructure (`core/`, `models/`, `store/`, `migrations/`)

| You are adding / changing… | It goes in… |
| --- | --- |
| A bootstrap setting (secret, path, port, repo), credential setting names | `core/config.py`, read from `.env` |
| The database engine, sessions and running migrations | `core/db.py` |
| Logging and secret redaction | `core/logging.py` |
| Audit kinds and outcomes, `AuditEntry`, the `AuditSink` type | `core/audit.py` |
| Password hashing and length limits, session and CSRF tokens | `core/security.py` |
| The UTC clock | `core/clock.py` |
| Running git on the controller: hardening flags, isolated env, redaction | `core/git.py`, the only git runner |
| A table / ORM entity | `models/<entity>.py` + a new migration in `migrations/versions/` |
| A column type shared by entities | `models/types.py` |
| The Alembic environment | `migrations/env.py` |
| Reading or writing runs and tasks | `store/runs_store.py` |
| Reading or writing settings, rule revisions, audit events | `store/settings_store.py`, `store/rules_store.py`, `store/audit_store.py` |

### GitHub and containers (`github/`, `runtime/`)

| You are adding / changing… | It goes in… |
| --- | --- |
| Any GitHub API call, clone, fetch or push | `github/gateway.py`, the only GitHub entry point |
| Git transport to GitHub and its one-shot auth header | `github/_git.py`, private to the gateway |
| GitHub App auth: API client and transport token | `github/_auth.py`, private to the gateway |
| Parsing GitHub API responses | `github/_parse.py`, private to the gateway |
| The action, payload and check of every GitHub write, and the check → audit → dry-run sequence | `github/write_guard.py`, shared with the test fake |
| Fetching the data the trust rules need | `github/trust.py` |
| Reading rulesets and App permissions for preflight | `github/preflight.py` |
| Starting, exec'ing and stopping containers, runtime health, labels, proxy env | `runtime/containers.py` |
| Task directory layout and safe removal | `runtime/task_dirs.py` |
| Controller-side git work: copies, applying patches, commits, diff summaries | `runtime/controller_git.py` |
| Reading the target repo's docs from the controller clone | `runtime/repo_docs_reader.py` |
| Egress allowlist rendering and reload | `runtime/egress.py` |
| Secret scanning of a patch (gitleaks) | `runtime/secret_scan.py` |
| The agent, runner and proxy images | `runtime/images/agent/`, `runtime/images/runner/`, `runtime/images/egress-proxy/` |
| Docker install and dockerd start shared by the agent and runner images | `runtime/images/common/` |

### Agents and classifiers (`agents/`, `classifiers/`)

| You are adding / changing… | It goes in… |
| --- | --- |
| The backend interface (`AgentBackend`, `SessionRequest`, `SessionOutcome`, `BackendSpec`) | `agents/base.py` |
| Running one session through an executor with transcript capture | `agents/session.py` |
| A specific agent CLI (argv, env, event parsing, its `BackendSpec`) | `agents/backends/<name>.py`, registered in `agents/registry.py` |
| What every backend shares (`CliBackend`, auth modes, provider table, outcome ladder) | `agents/backends/_shared.py` |
| Checking agent settings and credentials against the backends | `agents/registry.py` |
| A prompt template | `agents/prompts/<name>.md.j2`, rendered by `agents/prompting.py` |
| A default work rule and the rules in effect | `agents/rules/<role>.md`, loaded by `agents/rules.py` |
| The classifier interface, the shared HTTP call and `HttpClassifier` | `classifiers/base.py` |
| A specific classifier API | `classifiers/<name>.py`, registered in `classifiers/registry.py` |

### Runs (`pipeline/`, `scheduler/`)

| You are adding / changing… | It goes in… |
| --- | --- |
| Run lifecycle: lock, current step, kill, startup reconcile | `pipeline/run_controller.py` |
| One run: setup steps, candidates, selection, refines, worker slots, run end | `pipeline/run_flow.py` |
| Cleanup after a kill or restart | `pipeline/recovery.py` |
| Preflight, required checks, egress, repo docs, rules, quotas | `pipeline/run_setup.py` |
| Candidate types and finding candidates | `pipeline/candidates.py` |
| Asking the classifier about candidates and repo output | `pipeline/classify.py` |
| The injected dependencies and the per-run context | `pipeline/deps.py` |
| Agent containers: spec, inbox, start and removal, read-only copies | `pipeline/sandbox.py` |
| Agent sessions, continues and handling their result | `pipeline/sessions.py` |
| How a job stops (escalate, skip, usage limit, failed session) | `pipeline/outcomes.py` |
| Writing task rows (state, cost, blockers, notes) | `pipeline/task_record.py` |
| The selector session | `pipeline/selector.py` |
| The refine session, its comment and label swap | `pipeline/refine_flow.py` |
| Starting the worker for an issue (fresh, replaced or continued branch) | `pipeline/worker_flow.py` |
| Choosing PR fixes and running them | `pipeline/pr_maintenance.py` |
| Merging the base branch into an existing branch | `pipeline/branch_update.py` |
| The worker job loop | `pipeline/job_engine.py` |
| Worker job identity and progress | `pipeline/worker_job.py` |
| The worker container and worker sessions | `pipeline/worker_session.py` |
| Turning the worker's patch into a commit and checking it | `pipeline/worker_policy.py` |
| Push, PR and CI wait | `pipeline/publish.py`, polling in `pipeline/ci_watcher.py` |
| The reviewer session and its verdict | `pipeline/worker_review.py` |
| Running `setup_command` and `check_command` | `pipeline/repo_commands.py` |
| Escalating an issue | `pipeline/escalation.py` |
| Text the controller writes to GitHub | `pipeline/texts.py` |
| Which task directories expire | `pipeline/retention.py` |
| Starting, recreating and stopping CI runners | `pipeline/runner_pool.py` |
| A scheduled trigger | `scheduler/scheduler.py` |

### UI and entry points (`services/`, `web/`, `cli.py`)

| You are adding / changing… | It goes in… |
| --- | --- |
| A UI use-case and its audit events (settings, flags, kill switch, rules, login, status) | `services/<name>_service.py`; its errors in `services/errors.py` |
| A UI page or partial | `web/routes/<page>.py` + `web/templates/`, one service call |
| A settings form page | `web/settings_page.py` (`SettingsPage.include`), fields via `web/forms.py` |
| What a page shows (view models) | `web/views/<page>.py` |
| Mapping errors to HTML (404, CSRF 403, 422 re-render) | `web/errors.py` |
| Login check, CSRF check, public paths, security headers | `web/security.py` |
| Rendering, navigation, flash messages | `web/rendering.py` |
| Pagination, CSV exports, transcript reading | `web/pagination.py`, `web/exports.py`, `web/transcripts.py` |
| What the UI needs from the run controller and runner pool | `web/ports.py` |
| The app factory and shared request state | `web/app.py`, `web/state.py` |
| A CLI command (`serve`, `hash-password`, `preflight`) | `cli.py` |

### Tests

| You are adding / changing… | It goes in… |
| --- | --- |
| A test | `tests/<package>/test_*.py`; names are unique across the suite |
| A fake for an external system | `tests/fakes/` |
| A shared test helper, factory or harness | `tests/support/`; rows via `tests/support/factories.py` |
| A check on the repo itself (layers, docs, routes, migrations) | `tests/guards/` |

---

## 1. Guiding principles

1. **Guardrails in code, not prompts.** Policy in `domain/` and capabilities (tokens, network, mounts) decide what is possible.
2. **Least capability.** Agents get a workspace and a model credential, nothing else.
3. **One home per concern, one source per fact.** Every concern is defined once and imported; three similar blocks are lifted into one.
4. **Unattended by default.** Nothing blocks on human input during a run; uncertainty becomes an escalation to `skeleton HITL`.
5. **Generic.** No assumptions about the target project's stack or the agent backend.
6. **Fail closed.** A failed preflight, an invalid agent output or a policy violation stops the action.
7. **Enforced, not described.** A rule in this document has a guard, a contract or a lint rule behind it (§7).

---

## 2. Topology

```text
VM (Ubuntu 24.04 or Debian 12, Docker + Sysbox)
└─ docker compose
   ├─ caddy         TLS → web UI on the LAN IP
   ├─ controller    FastAPI + APScheduler + SQLite; only holder of the GitHub App key; docker.sock
   ├─ egress-proxy  Squid with a domain allowlist; the only internet route for agent containers
   ├─ runner-proxy  the same image with the runners' own allowlist
   ├─ agent-*       one per session: sysbox-runc, internal network, non-root, resource limits
   └─ runner-*      optional self-hosted CI: one container per job, sysbox-runc, own internal network
```

- The data directory (`SC_DATA_DIR`) is mounted into the controller at the same path as on the host, so host paths can be handed to Docker.
- Self-hosted CI is reconciled by `pipeline/runner_pool.py`; when runners run and what they show is decided in `domain/runner_policy.py`.
- A runner gets only its own registration, mounted read-only; the image's job-started hook refuses branches outside `BRANCH_PREFIX`.

---

## 3. Packages and import rules

| Package | Responsibility | Must NOT |
| --- | --- | --- |
| `domain/` | Pure policy, state machine, schemas, settings model | Do I/O; import any other internal package |
| `core/` | Config, DB, logging, audit types, security primitives, clock, the git runner | Hold domain rules |
| `models/` | SQLAlchemy ORM entities | Hold logic |
| `store/` | Async data access | Hold business rules or audit policy |
| `github/` | The gateway, its private modules, write guard, trust data, preflight | Write without a `domain/` check and an audit event; offer a merge |
| `runtime/` | Containers, controller-side git, egress, secret scan, images | Pass a GitHub credential into a container |
| `agents/` | Backend interface, registry, backends, session runner, prompts, default rules | Call GitHub; import `runtime/` |
| `classifiers/` | Classifier interface, registry, providers | Call GitHub; decide anything |
| `pipeline/` | Everything a run does | Define policy of its own |
| `services/` | UI use-cases: validate, save, audit, commit | Import `web/`, `scheduler/`, `pipeline/` or the web framework |
| `scheduler/` | Cron triggers from settings | Contain run logic |
| `web/` | Routes, templates, view models | Contain business logic or queries |

Import direction (an arrow means "may import"):

```text
cli → web, scheduler → pipeline | services → github, runtime, agents, classifiers, store → models → core → domain
```

- `pipeline/` and `services/` never import each other; neither do `github/`, `runtime/`, `agents/`, `classifiers/` and `store/`.
- `web/` reaches a run only through `web/ports.py`; from `agents/` it imports only `agents.registry`, from `classifiers/` only `classifiers.registry`.
- `github/_git.py`, `github/_auth.py` and `github/_parse.py` are imported only by `github/gateway.py`.
- Each external library has one home: `githubkit` in `github/`, `docker` in `runtime/`, `httpx` in `classifiers/`, `apscheduler` in `scheduler/`, `fastapi` in `web/`.

---

## 4. Run flow

What happens in each stage is described in [docs/run-flow.md](docs/run-flow.md); this section only says who owns it.

| Stage | Owner |
| --- | --- |
| Trigger and lifecycle | `scheduler/scheduler.py`, `pipeline/run_controller.py` |
| Preflight and setup | `pipeline/run_setup.py`, rules in `domain/repo_policy.py` and `agents/registry.py` |
| Candidates and screening | `pipeline/candidates.py`, `pipeline/classify.py`, rules in `domain/candidate_policy.py` and `domain/trust_policy.py` |
| Selection | `pipeline/selector.py` |
| Refine | `pipeline/refine_flow.py` |
| Worker job | `pipeline/worker_flow.py` → `pipeline/job_engine.py` |
| PR maintenance | `pipeline/pr_maintenance.py` → `pipeline/branch_update.py` → `pipeline/job_engine.py` |
| Stop and recovery | `pipeline/outcomes.py`, `pipeline/recovery.py` |

Worker task states (source of truth: `domain/task_state.py`):

```text
        STARTED     PREPARED  CODE_DONE      POLICY_PASSED  CI_PASSED   REVIEW_APPROVED
QUEUED ───────→ PREPARING ──→ CODING ──→ POLICY_CHECK ──→ AWAITING_CI ──→ REVIEWING ──→ DONE
                                ↑            │                  │              │
                                └────────────┴──────────────────┴──────────────┘
                 POLICY_FAILED / CHECK_FAILED, CI_FAILED, REVIEW_CHANGES (until rounds run out)
any non-terminal state → ESCALATED (ESCALATE) | FAILED (ERROR) | ABORTED (ABORT)
```

Invariants (each has a test):

- The controller never runs git in a directory an agent can write to; the agent's diff is read from inside the container and applied in the controller clone.
- Policy checks and the secret scan run on the commit before every push; hard checks are never contestable.
- Every push carries a lease; a branch is continued only when its head is the last commit the controller pushed in a live run.
- A dry run executes every session but the gateway only audits its writes.

---

## 5. Data model

SQLite in the data directory; migrations in `migrations/versions/` are applied at startup.

| Entity | Purpose |
| --- | --- |
| `SettingsRow` | One row holding the UI-editable settings as JSON, validated by `domain/settings.py` |
| `RuleRevision` | Append-only history of each role's work rules; the newest revision is active |
| `Run` | One scheduled or manual run: trigger, `dry_run`, state, timestamps, note |
| `WorkerTask` | One issue in one run: kind (`issue`, `pr_fix`), state, branch, PR, SHAs, session, rounds, cost, note, blockers |
| `RefineTask` | One refine in one run: state, session id, cost, error |
| `AuditEvent` | Append-only: kind and outcome from `core/audit.py`, summary, payload, run |

Task directories live under `<data>/tasks/<key>/`; rows and the audit log are never pruned.

---

## 6. Cross-cutting

### 6.1 Configuration

- **Bootstrap** (`core/config.py`, prefix `SC_`, from `.env`): never editable in the UI and never logged. The variables are listed in `.env.example` and `docs/install.md`.
- **Runtime** (`domain/settings.py`, edited in the UI): saved through `services/settings_service.py`.
- The domain checks only the shape of agent fields; `agents/registry.py` and `classifiers/registry.py` check them against what exists, on save and in preflight.

### 6.2 Agent backends

- A backend turns a `SessionRequest` into an argv and env, and the CLI output into a `SessionOutcome`; `agents/session.py` runs it through an injected executor.
- Output is always validated by `domain/schemas.py`, whether or not the backend enforces a schema itself.
- A backend receives only the credentials its `BackendSpec` declares.
- Backends: `claude_code` (default), `codex`, `gemini`, `opencode`, each in `agents/backends/<name>.py`.

Adding a backend:

1. A module `agents/backends/<name>.py`, usually a `CliBackend` subclass, exporting its `BackendSpec`.
2. One entry for that spec in `agents/registry.py`.
3. The CLI install in `runtime/images/agent/Dockerfile`, pinned, with a checksum where the CLI ships release binaries.
4. Its credentials in `.env.example` and `docs/install.md` (a guard checks `.env.example`).

### 6.3 Work rules and precedence

Each session gets `common.md` + `<role>.md` + the repo docs as its system prompt, in this order of authority:

1. Code policy (`domain/`): cannot be overridden.
2. Skeleton work rules: process.
3. Target repo's docs: code standards.

Untrusted text (issue bodies, comments, CI logs) is rendered only inside delimited `<untrusted …>` blocks by `agents/prompting.py`.

### 6.4 Audit

- Every GitHub write attempt is recorded through `github/write_guard.py`, whether it is allowed, blocked or a dry run.
- Logins, settings and rules changes and the kill switch are audited by `services/`; run events by `pipeline/`.
- All of it goes through `store/audit_store.py`; the kinds live in `core/audit.py`. Agents cannot read or write the audit log.

### 6.5 Classifiers

- An optional classifier answers fixed multiple-choice questions on the controller, never in a container, and replaces no agent.
- `domain/classifier_policy.py` holds the questions, validates every answer and decides; `pipeline/classify.py` only asks.
- Providers: `jev` (default) and `openai_decisions`, each in `classifiers/<name>.py`.
- Adding one: a module with an `HttpClassifier` subclass and its `ClassifierSpec`, one entry in `classifiers/registry.py`, its credential in `.env.example`.

### 6.6 Tests

- Tests never make real network calls; every external system has a fake in `tests/fakes/` that imports the production rule instead of copying it.
- Run and task rows are seeded through `tests/support/factories.py`; helpers live in `tests/support/`, never next to the tests.
- Every policy gets adversarial tests: the forbidden action, an edge case and a bypass attempt.

---

## 7. Modularity contract

Every change satisfies all of these. Baselines and limits only ever shrink.

| Rule | Enforced by |
| --- | --- |
| Import direction of §3; private gateway modules; one home per external library | import-linter (`[tool.importlinter]` in `pyproject.toml`) |
| No module-level import cycle | `tests/guards/test_import_cycles.py` |
| No I/O in `domain/` | `tests/guards/test_domain_purity.py` |
| No merge, approve or branch-delete path in `github/` | `tests/guards/test_no_merge_path.py` |
| Every route is behind login, every unsafe method behind CSRF | `tests/guards/test_route_auth_coverage.py` |
| Migrations form one numbered chain | `tests/guards/test_migrations_linear.py` |
| Comments are one line, no docstrings, files stay small | `tests/guards/test_architecture.py` |
| Rows via factories, helpers in `tests/support/`, no shadowed fixtures | `tests/guards/test_test_conventions.py` |
| This document names every module and only existing paths, and stays short | `tests/guards/test_docs_contract.py` |
| Small functions, no lazy imports, no commented-out code, no `TODO` | ruff (`[tool.ruff.lint]` in `pyproject.toml`) |
| No copy-pasted blocks | jscpd (`.jscpd/`, the `static` job in `.github/workflows/ci.yml`) |

Not enforced by a tool, so check by hand:

- No GitHub write without a `domain/` policy check and an audit event.
- No credential passed into a container except the backend's model credential or the runner's own registration.
- Agent output parsed only through `domain/schemas.py`.
- No backend-specific code outside `agents/backends/`, no classifier-specific code outside `classifiers/`.

| Wrong | Right |
| --- | --- |
| A prompt says "never add label `skeleton`" as the only defence | `domain/label_policy.py` rejects it |
| `pipeline/` calls `githubkit` directly | `pipeline/` calls `github/gateway.py` |
| A worker container gets `GH_TOKEN` to push | The controller applies the patch in its own clone and pushes |
| `if backend == "claude"` in `pipeline/` | Behaviour behind `AgentBackend` |
| A route loads, validates, saves and audits inline | One call to `services/<name>_service.py` |
| A threshold or "is this allowed" check inside `pipeline/` | A pure function in `domain/` with a test |
| A fake re-types the payload production builds | Both use the same builder (`github/write_guard.py`) |
| A second copy of a constant, pattern or text | Import the one definition |

---

## 8. When to update this document

- A module is added, moved or removed: its row in §0, in the same change (the docs guard names what is missing).
- A package is added: §3 and the structure block in `AGENTS.md`.
- A rule gets or loses its enforcement: §7.
- An entity is added: §5.

Add only the line that says where a piece lives and what it is for. Behaviour belongs in a test; what a run does belongs in `docs/run-flow.md`.
Never add changelogs, task notes or history here.
