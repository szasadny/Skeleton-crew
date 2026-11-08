# Skeleton Crew: Architecture

The authoritative reference for where things live and which rules hold. Behaviour details live in code and tests, not here. Grep for `## <n>.` and update the section you change.

| § | Contents |
| --- | --- |
| 0 | Lookup table: where does X go? |
| 1 | Guiding principles |
| 2 | Topology |
| 3 | Packages and import rules |
| 4 | Run flow, task lifecycle, code path from agent to PR |
| 5 | Data model |
| 6 | Cross-cutting: config, agent backends, work rules, audit, classifiers |
| 7 | Modularity contract |
| 8 | When to update this document |

---

## 0. Lookup: where does X go?

Each row has exactly one correct place. Putting it anywhere else violates the contract (§7).

| You are adding / changing… | It goes in… |
| --- | --- |
| Which label changes are allowed | `domain/label_policy.py` |
| Branch naming, the allowed push target, whether an existing branch may be continued | `domain/branch_policy.py` |
| Protected paths, diff size, file-mode rules | `domain/diff_policy.py` |
| Parsing the added lines of a diff | `domain/diff_lines.py` |
| Conflict-resolution rules (only conflicted files, no conflict markers) | `domain/merge_policy.py` |
| Detecting long code comments in a diff | `domain/comment_policy.py` |
| Task directory key formats (`worker-<id>`, `refine-<id>`, `run-<id>-selector`) | `domain/task_keys.py` |
| Findings, objections and contest matching | `domain/contest_policy.py` |
| A contestable (grey-area) check | A pure function in its policy module, registered in `domain/contestable_checks.py` |
| Sanitising agent text before it reaches GitHub (length cap, @mentions, closing keywords) | `domain/text_policy.py` |
| Deciding whether issue content is trusted (pure rules) | `domain/trust_policy.py` |
| A task, refine or run state, or a transition | `domain/task_state.py` |
| The JSON an agent role must return, and its cross-checks | `domain/schemas.py` (Pydantic, `extra="forbid"`) |
| Which target-repo docs an agent gets (instruction docs, their links, caps) and how they are rendered | `domain/repo_docs.py` |
| A UI-editable setting | `domain/settings.py` |
| Any GitHub API call, clone, fetch or push | `github/gateway.py`: the only GitHub entry point |
| Git transport to GitHub (clone, fetch, ls-remote, push) and its auth-header env | `github/_git.py`, used only by the gateway |
| Pure CI evaluation: check states, required checks, job-log formatting | `github/_ci.py`, re-exported by the gateway |
| Fetching the data the trust rules need | `github/trust.py` |
| Ruleset and App-permission checks | `github/preflight.py` |
| The policy check → audit → dry-run sequence every GitHub write runs | `github/write_guard.py` (shared by the gateway and its test fake) |
| Starting, exec'ing, stopping containers; runtime health | `runtime/containers.py` |
| Controller-side git work: worker copies, applying patches, diff summaries, reading the repo docs | `runtime/workspace.py` |
| Pruning expired task directories | `pipeline/retention.py` (which keys expire), `runtime/workspace.py` (safe removal) |
| Egress allowlist rendering and reload | `runtime/egress.py` |
| Which repos may use self-hosted runners, runner names, restart backoff, runner status | `domain/runner_policy.py` |
| Starting, recreating and stopping the CI runner containers; their preflight checks | `pipeline/runner_pool.py` |
| The CI runner image (actions runner, nested Docker, register and run entrypoint, the job-started hook that refuses non-skeleton branches) | `runtime/images/runner/` |
| Secret scanning of a patch (gitleaks) | `runtime/secret_scan.py` |
| The agent backend interface and the session runner | `agents/base.py`, `agents/session.py` |
| A specific agent CLI (argv, env, output parsing, its `BackendSpec`) | `agents/backends/<name>.py`, registered in `agents/registry.py` |
| Helpers every backend shares (stdin wrapper, login-file writer, JSONL reading, safe ids) | `agents/backends/_shared.py` |
| Checking agent settings against the backends (model, effort, options, models per option) | `agents/registry.py` (`field_problems`, `settings_problems`) |
| The classifier interface and the shared HTTP call | `classifiers/base.py` |
| A specific classifier API (request, response, its `ClassifierSpec`) | `classifiers/<name>.py`, registered in `classifiers/registry.py` |
| The classifier questions and what their answers decide (thresholds, fail closed or fall back) | `domain/classifier_policy.py` |
| Asking the classifier about candidates and repo output | `pipeline/classify.py` |
| A role prompt template | `agents/prompts/<role>.md.j2`, rendered by `agents/prompting.py` |
| A default work rule | `agents/rules/<role>.md` (seed for the UI-editable copy) |
| Reading or writing settings, rules, runs, tasks | `store/` |
| Validating a work-rules revision (known key, non-empty, size cap) | `store/rules_store.py` |
| Run orchestration: preflight order, selection, worker slots | `pipeline/run_controller.py`, setup steps (rules, repo docs) in `pipeline/run_setup.py` |
| Starting the worker for a new issue (fresh or existing branch) | `pipeline/worker_flow.py` |
| The worker job loop: coding, repo checks, push, CI wait, review rounds, job outcome | `pipeline/job_engine.py` |
| Worker job state (git SHAs, rounds, reviewer session, follow-up kinds) | `pipeline/worker_job.py` |
| Turning the worker's patch into a commit and running the policy checks on it | `pipeline/worker_policy.py` |
| The reviewer session of a worker job | `pipeline/worker_review.py` |
| Running `setup_command` and `check_command` in the worker container | `pipeline/repo_commands.py` |
| Starting agent containers and running sessions, including continues | `pipeline/sessions.py` |
| Escalating an issue (audit, comment, label swap) | `pipeline/escalation.py` |
| Text the controller writes to GitHub (commit messages, PR body, comments) | `pipeline/texts.py` |
| Merging the base branch into an existing branch (clean merge or conflict) | `pipeline/branch_update.py` |
| Choosing PR maintenance jobs (ownership, mergeability) and running conflict fixes | `pipeline/pr_maintenance.py` |
| Refine flow, selector, CI wait | `pipeline/refine_flow.py`, `pipeline/selector.py`, `pipeline/ci_watcher.py` |
| A bootstrap setting (secret, path, port, repo) | `core/config.py`, read from `.env` |
| A table / ORM entity | `models/<entity>.py` + Alembic migration in `migrations/versions/` |
| Audit kinds and outcomes | `core/audit.py` |
| Writing or listing audit events | `store/audit_store.py` |
| Password hashing, session and CSRF helpers | `core/security.py` |
| Running git on the controller: hardening flags, isolated env, error redaction | `core/git.py`, the only git runner |
| A UI page or partial | `web/routes/<page>.py` + `web/templates/` |
| A scheduled trigger | `scheduler/` |
| A CLI command (`serve`, `hash-password`, `preflight`) | `cli.py` |
| A test fake for an external system | `tests/fakes/` |

---

## 1. Guiding principles

1. **Guardrails in code, not prompts.** Prompts guide quality. Policy in `domain/` and capabilities (tokens, network, mounts) decide what is possible.
2. **Least capability.** Agents get a workspace and a model credential, nothing else. The controller holds every other credential.
3. **Single source of truth.** Every concern is defined once and imported.
4. **Unattended by default.** Nothing ever blocks on human input during a run. Uncertainty becomes an escalation to `skeleton HITL`.
5. **Generic.** No assumptions about the target project's stack or the agent backend. Code standards come from the target repo's `CLAUDE.md` / `AGENTS.md`.
6. **Fail closed.** A failed preflight, an invalid agent output or a policy violation stops the action. It never falls back to a weaker path.

---

## 2. Topology

```text
VM (Ubuntu 24.04 or Debian 12, Docker + Sysbox)
└─ docker compose
   ├─ caddy         TLS → web UI on the LAN IP
   ├─ controller    FastAPI + APScheduler + SQLite; only holder of the GitHub App key; docker.sock
   ├─ egress-proxy  Squid with a domain allowlist; the only internet route for agent containers
   ├─ runner-proxy  the same image with the runners' own allowlist; the only internet route for runners
   ├─ agent-*       spawned per session: runtime=sysbox-runc, internal network, non-root, resource limits; handed out only once the entrypoint is ready
   └─ runner-*      optional self-hosted CI: one container per registered runner while a task waits for CI, runtime=sysbox-runc, own internal network, recreated after every job
```

Self-hosted CI (`pipeline/runner_pool.py`) reconciles every few seconds. Runners run only when `ci.enabled` is on, the kill switch is off, the repo is private and at least one worker task of a live (not dry) run is `AWAITING_CI`. Then each runner with a registration in `<data>/runners/runner-<n>/` gets a container that runs one job (`run.sh --once`) and exits, and the pool recreates it from the image. Otherwise no runner container exists: the pool shows `standby` when only the demand is missing and `offline` with the reason otherwise. The registration is mounted read-only and copied in, so a job cannot change what the next job starts from. Before every job, the image's job-started hook fails any job whose branch does not start with `domain/branch_policy.BRANCH_PREFIX` (passed in as `SC_BRANCH_PREFIX`), so the runners only run Skeleton Crew's own pull requests. Runners are registered once by hand (`docs/install.md`); the App never creates registration tokens.

The data directory (`SC_DATA_DIR`) is bind-mounted into the controller at the **same path** as on the host, so the controller can hand host paths to Docker for agent mounts.

---

## 3. Packages and import rules

All code lives in `src/skeleton_crew/`.

| Package | Responsibility | Must NOT |
| --- | --- | --- |
| `domain/` | Pure policy, state machine, schemas, settings model | Do I/O; import any other internal package |
| `core/` | Config, DB engine/session, logging with redaction, audit enums, security primitives, the hardened git runner | Hold domain rules |
| `models/` | SQLAlchemy ORM entities | Hold logic |
| `store/` | Async data access for settings, rules, runs, tasks, audit | Hold business rules |
| `github/` | Gateway (allow-listed operations only, **no merge**), its private git transport and CI evaluation, write guard, trust data, preflight | Write without the `domain/` policy check and an audit event |
| `runtime/` | Containers, controller-side git work, egress, secret scan | Pass any GitHub credential into a container |
| `agents/` | Backend interface, backend registry, backends, session runner, prompts, default rules | Call GitHub; import `runtime/` (it receives an executor) |
| `classifiers/` | Optional classifier interface, registry, providers (controller-side HTTP) | Call GitHub; decide anything (`domain/` does) |
| `pipeline/` | Run controller, worker flow, job engine, branch update, PR maintenance, refine flow, CI watcher, retention | Define policy of its own |
| `scheduler/` | Cron triggers from settings, manual "run now" | Contain run logic |
| `web/` | FastAPI routes, Jinja2 templates, HTMX partials | Contain business logic |

Import direction (an arrow means "may import"):

```text
cli → web, scheduler → pipeline → github, runtime, agents, classifiers, store → models → domain
every package → core (core → domain only)
```

`github/`, `runtime/`, `agents/`, `classifiers/` and `store/` never import each other. `pipeline/` composes them. import-linter enforces this in CI.

`github/_git.py` and `github/_ci.py` are private to the gateway: only `github/gateway.py` imports them, enforced by import-linter. Other packages import the CI names from `github/gateway.py`.

`web/` reaches the run controller only through the `web/ports.py` `RunControl` protocol and may import `scheduler/` to re-apply the schedule. Its only `agents/` imports are `agents.rules.default_rules`, for "reset to default" on the rules page, and `agents.registry`, to render and check the agents page; likewise its only `classifiers/` import is `classifiers.registry`.

---

## 4. Run flow, task lifecycle, code path

### 4.1 Run flow

1. **Preflight**: a ruleset on the base branch requires a PR plus ≥1 approval, and the App cannot bypass it (without any ruleset only when `allow_unprotected_base_branch` is on); the App has the required permissions, no `workflows` permission and no `administration` write; the agent settings pass `agents/registry.py`; Docker, Sysbox, the agent image and the proxy are healthy; each configured backend has its credential; with self-hosted CI on, the repo is private, the runner image, network and proxy are healthy and at least one runner is registered; the required checks can be read, the egress allowlist applied and the repo docs read (§6.3) from a clone of the base branch that the selector reuses. Any failure ends the run as `preflight_failed`. While the kill switch is on, no run starts.
2. **Candidates**: open issues with the labels that pass the trust rules (`docs/labels.md` § Trust), except issues that also carry `skeleton HITL` (or, for `skeleton`, `skeleton refine`). Issues with an open skeleton PR go to PR maintenance instead. Expired task directories are pruned before this step (§5). With a classifier use case on, each candidate is screened first (§6.5).
3. **Selector**: orders candidates up to the daily quota. It escalates only issues that are clearly too large, need an owner decision (with the question and options) or are rejected; every other candidate it does not pick stays queued.
4. **Worker pool** (≤ `max_parallel_workers`), and in parallel the **refine flow**. PR maintenance jobs (§4.4) take slots before new issues.
5. **Stop** when candidates or quota run out, the window ends, or the kill switch fires.

**Dry run** (default on): every agent session runs for real, but the gateway only audits its writes. Workers skip the CI wait and go straight to review, so one dry run exercises selector, worker, policy checks, reviewer and refiner without touching GitHub.

### 4.2 Worker task states

Source of truth: `domain/task_state.py`.

```text
        STARTED     PREPARED  CODE_DONE      POLICY_PASSED  CI_PASSED   REVIEW_APPROVED
QUEUED ───────→ PREPARING ──→ CODING ──→ POLICY_CHECK ──→ AWAITING_CI ──→ REVIEWING ──→ DONE
                                ↑            │                  │              │
                                └────────────┴──────────────────┴──────────────┘
                 POLICY_FAILED / CHECK_FAILED, CI_FAILED, REVIEW_CHANGES (until rounds run out)
any non-terminal state → ESCALATED (ESCALATE) | FAILED (ERROR) | ABORTED (ABORT)
```

While a task is `AWAITING_CI` its container stays alive, but no session runs, so waiting costs no tokens. With self-hosted CI on, this state is what starts the runners (§2). CI counts as passed only when every GitHub Actions workflow run for the commit has completed and every check the base-branch ruleset requires has reported green; a required check that never appears ends in the CI timeout. If nothing reports within `ci_grace_minutes`, the task escalates when `require_ci` is on and goes to review otherwise. A CI failure or a review finding resumes the same agent session. Each kind of round has its own limit: a failed `check_command` fires `CHECK_FAILED` and counts against `max_check_rounds`, red CI against `max_ci_rounds`, a policy violation (`POLICY_FAILED`) against `max_policy_rounds` and a reviewer rejection against `max_review_rounds`. When the worker's time runs out while it waits for CI, the escalation says so. A clean merge of the base into a continued branch has no coding step: it goes from `CODING` straight to `POLICY_CHECK`.

### 4.3 From agent work to PR

The controller never runs git inside a directory an agent can write to: a planted `.git/config` or hook would execute on the controller.

1. The gateway clones the repo into `<data>/tasks/<key>/controller/` (credentials via a one-shot header, never stored). This clone is never mounted into a container.
2. `runtime/workspace` makes a plain local copy at `<data>/tasks/<key>/agent/` and mounts it at `/workspace`. If `setup_command` is set, the controller execs it in the worker container before the first session; failure escalates.
3. After a session, the controller execs `check_command` in the container if set, except while resolving a merge conflict; a failure goes back to the worker with the output tail. Repo commands run as the agent user with no extra env, so they never see the model credential. Then it execs `git diff --binary <base>` **inside the container** (external diff and textconv disabled) and reads it as plain bytes.
4. In the controller clone, with hooks and fsmonitor disabled, it applies the patch on `<base>` and creates a commit whose tree is "base + patch" and whose parent is the current branch tip. Commit metadata comes from the controller, not the agent.
5. `diff_policy`, gitleaks and the checks in `domain/contestable_checks.py` run on that commit versus base. A contestable finding is a violation unless the worker contested it in `objections` with a reason, matched on check, path and line; contested findings go to the reviewer, who decides. Hard checks are never contestable. Only then does the gateway push `skeleton/<n>-<slug>` and open or update the PR.

Reviewer, refiner and selector sessions get a fresh read-only copy. Within one worker job the reviewer keeps its container and session: a later review round resumes it, with the copy refreshed in place to the new head and the changes since its last review in `/skeleton-in/since-review.diff`. Every review must list in `missing_doc_updates` each doc that should have changed but did not (an empty list means none); `domain/schemas.py` rejects `approve` while that list is non-empty, so a missing doc update always goes back to the worker.

Workers and reviewers must list in `blockers` whatever hindered doing or verifying the work (missing tool, blocked domain, for workers also checks that could not run), also when worked around. Reviewers trust CI and run no checks themselves. The task row keeps them all, the PR body lists the worker's blockers known when the PR opens, and each review check lists the round's worker and reviewer blockers.

The first push of a new branch uses a lease that requires the branch not to exist. Each run lists open PRs once (`open_skeleton_prs`); an issue with more than one open skeleton PR is skipped for new work and maintenance, with an audit event. If `skeleton/<n>-<slug>` already exists without an open PR when a worker starts, the controller decides before any write (`domain/branch_policy.continuation_problem`):

- The head is not the last commit the controller pushed in a live run (`runs_store.last_pushed_task`): escalate ("the branch has commits Skeleton Crew did not push"). Foreign content is never adopted, so it never skips the per-push secret scan.
- The branch has a PR that was closed without merging: escalate. A closed PR is never resurrected.
- The branch has no changes of its own (its merge with the base equals the base, e.g. after a merged PR whose branch was kept): it is replaced by a normal worker run, with a lease on its old head.
- Otherwise (a crash before the PR was opened) the worker comments on the issue and continues from that branch: the controller merges the base into it as in §4.4 (a clean merge is pushed as a merge commit, a conflict goes to a worker), then CI and a fresh review follow and the PR is opened.

### 4.4 PR maintenance

`pipeline/pr_maintenance.py` resolves merge conflicts in open skeleton PRs while their issue still has `skeleton`. The merge itself lives in `pipeline/branch_update.py`, shared with continued branches (§4.3); the job loop after it is `pipeline/job_engine.py`. PR maintenance runs even when the selector fails.

1. A PR qualifies only when its head is the last commit the controller pushed in a live run (`runs_store.last_pushed_task`). Any other head means a human took over; the PR is skipped with an audit event.
2. While fix quota is left, the controller reads GitHub's `mergeable` flag. `true`: skipped with an audit event, no task and no clone; a PR that is only behind stays as it is. `null` (GitHub still computing): polled a few times, then skipped with an audit event. `false`: a `pr_fix` task is created and takes one `conflict_fixes_per_day`.
3. The controller merges the base into the branch in its own clone with `git merge-tree`, so history is kept and nothing is rebased.
4. **Conflict**: the conflicted tree becomes a local-only commit for the agent copy. The worker's patch on top of it becomes a merge commit with parents (old head, base). It may only touch conflicted files and must leave no conflict markers. Then CI and a fresh review follow, as for a new PR. Failure escalates the issue as usual.
5. If the local merge turns out clean after all (a race with GitHub), the task ends `aborted` without writes or agent; its quota stays used.

---

## 5. Data model

SQLite in the data directory, with Alembic migrations applied at startup.

| Entity | Purpose |
| --- | --- |
| `SettingsRow` | One row holding the UI-editable settings as JSON, validated by `domain/settings.py` |
| `RuleRevision` | Append-only history of each role's work rules; the newest revision is active |
| `Run` | One daily or manual run: trigger, `dry_run`, state, timestamps, note |
| `WorkerTask` | One issue in one run: kind (`issue`, `pr_fix`; `pr_update` only on historical rows), state, branch, PR, base and head SHA, session id, container id, round counters, cost, `note` (why it escalated, aborted or failed), `blockers` (JSON list of role-prefixed blockers from every worker and reviewer session, deduplicated and capped) |
| `RefineTask` | One refine in one run: state, session id, cost, error |
| `AuditEvent` | Append-only: kind and outcome from `core/audit.py`, summary, payload, run |

Task directories (`<data>/tasks/<key>/`, transcripts and command logs) of terminal tasks and finished runs are pruned after each passed preflight once older than `task_retention_days`; their rows and the audit log are never pruned.

---

## 6. Cross-cutting

### 6.1 Configuration

- **Bootstrap** (`.env`, `core/config.py`, prefix `SC_`): target repo `owner/name`, GitHub App id and key path, optional installation id and bot login, agent credentials (`SC_AGENT_CREDENTIALS__<NAME>`, read into `agent_credentials` with lowercase names), UI password hash, session secret, data directory, image, network and egress-proxy names (for agents and for runners), listen address. Never editable in the UI and never logged.
- **Runtime** (`domain/settings.py`, edited in the UI): schedule, limits, per-role agent (backend, model, effort, web tools), per-backend options (`backend_options.<backend>.<option>`), label names, base branch, protected paths, `setup_command`, `check_command`, `require_ci`, `allow_unprotected_base_branch`, trusted users, egress allowlist, resource limits, self-hosted CI (`ci.*`: on/off, runner count, labels, runner allowlist, no-proxy hosts, runner resource limits), classifier (`classifier.*`: provider, model, one toggle per use case, routed efforts), `dry_run`, kill switch. The domain only checks the shape of agent fields; `agents/registry.py` checks them against the backends on save and in preflight. The domain's default agent strings must match a registry backend, which a test enforces.

### 6.2 Agent backends

`agents/base.py` defines `AgentBackend`. A backend turns a `SessionRequest` (role, prompt and the inbox file that holds it, model, effort, limits, schema, rules file, resume id) into an argv plus environment; the prompt is read from that file, never passed as an argument, because Linux caps one argument at 128 KiB, and turns the CLI output into a `SessionOutcome` (session id, raw structured output, cost, turns, error flags, early-stop reason). It also declares the egress domains and credentials it needs, and the repo docs its CLI loads by itself (`auto_loaded_docs`, e.g. `CLAUDE.md` and `.claude/CLAUDE.md` for Claude Code). Each backend module exports a `BackendSpec` (name, display name, models, efforts, options with allowed values and defaults, credential names, web-tools support, factory). `agents/registry.py` holds the specs: it builds a backend with only its declared credentials and its options, and rejects an unknown backend, model, effort, option or unsupported web tools, on UI save (`field_problems`) and in preflight (`settings_problems`; a run then ends `preflight_failed`). `agents/session.py` runs any backend through an injected executor with transcript capture; the executor (`runtime/containers.py`) enforces the wall-clock timeout with SIGINT and a hard kill, so backends add no timeout wrapper of their own. Output is always validated by `domain/schemas.py`, whether or not the backend enforces a schema itself. The target repo's own agent hooks run as configured. When a session ends cleanly without a result, for example because a hook stopped the turn, the backend reports it as `stopped_early` and `pipeline/sessions.py` resumes the same session with a continue prompt, up to `max_continues` times, then fails closed. A continue only gets what is left of the session's `max_turns`, `session_timeout_minutes` and budget, and none is started once one of them is used up.

Backends (`claude_code` is the default):

- **Claude Code** (`agents/backends/claude_code.py`): `claude -p` with `--output-format stream-json`, `--json-schema`, `--resume`, `--max-turns`, `--disallowedTools` for every interactive tool, and `--max-budget-usd` in API-key mode. Option `auth_mode` is `subscription` (`SC_AGENT_CREDENTIALS__CLAUDE_OAUTH_TOKEN` from `claude setup-token`, passed as `CLAUDE_CODE_OAUTH_TOKEN`) or `api_key` (`SC_AGENT_CREDENTIALS__ANTHROPIC_API_KEY`, passed as `ANTHROPIC_API_KEY`), always with the unmodified CLI.
- **Codex** (`agents/backends/codex.py`): `codex exec --json` (`exec resume <id>` to resume), `--dangerously-bypass-approvals-and-sandbox`, `-c web_search="disabled"` unless web tools are on. It has no turn limit, so the session timeout is its only bound, and its strict schema mode rejects optional fields, so it gets no CLI schema. Option `auth_mode` is `api_key` (`OPENAI_API_KEY`, passed as `CODEX_API_KEY`) or `subscription` (`CODEX_AUTH_JSON`, written to `~/.codex/auth.json`).
- **Gemini CLI** (`agents/backends/gemini.py`): headless `gemini` with `--output-format stream-json`, `--approval-mode yolo`, `--skip-trust`, `--resume <id>`, and `--allowed-mcp-server-names` with a name no server has, so repo MCP servers stay off. The turn limit (`maxSessionTurns`), excluded tools (interactive always, web unless enabled) and auth type are written to `~/.gemini/settings.json` before each session. Effort is `default` only. Option `auth_mode` is `api_key` (`GEMINI_API_KEY`) or `subscription` (`GEMINI_OAUTH_CREDS_JSON`, written to `~/.gemini/oauth_creds.json`).
- **OpenCode** (`agents/backends/opencode.py`): `opencode run --format json` with `-m <provider>/<model>`, `--variant` for effort and `--session` to resume. A per-session `OPENCODE_CONFIG_CONTENT` denies the `question` tool and, unless web tools are on, `webfetch`/`websearch`, loads the rules file as `instructions` and sets the agent's `steps`, which OpenCode treats as advisory. Option `provider` (`anthropic`, `openai`, `openrouter`, `google`) picks one key and one egress host, and limits the models to that provider's prefix (`BackendSpec.model_filter`). Catalog fetches, auto-update and CLAUDE.md compatibility are off in the image.

Every backend builds its argv with `agents/backends/_shared.py`: the prompt (and, for CLIs without a system-prompt file option, the rules before it) goes to stdin, a subscription login file is written from `SC_AUTH_FILE` only when absent (the CLI may have refreshed it), and a per-session settings file is rewritten each time. The schema is also in the inbox as `schema.json` for CLIs that take it as a file.

Adding a backend:

1. A module `agents/backends/<name>.py` implementing `AgentBackend` and exporting its `BackendSpec`.
2. One entry for that spec in `agents/registry.py`.
3. The CLI install in `runtime/images/agent/Dockerfile`, pinned, with a checksum where the CLI ships release binaries.
4. Its credentials as `SC_AGENT_CREDENTIALS__<NAME>` in `.env`, documented in `.env.example` and `docs/install.md`.

No other package changes: the UI, preflight and pipeline read everything from the registry.

### 6.3 Work rules and precedence

Each session gets `common.md` + `<role>.md` + the repo docs as its appended system prompt, in this order of authority:

1. Code policy (`domain/`): cannot be overridden.
2. Skeleton work rules: process.
3. Target repo's docs: code standards.

The repo docs are read once per run from the base branch (`domain/repo_docs.py`): every tracked `CLAUDE.md` and `AGENTS.md`, `.claude/CLAUDE.md`, and the tracked `.md` files their relative links point to, one level deep. Docs the role's backend already loads itself are left out, but their links still count. Each doc is fenced, and docs over the size or count caps are listed by path instead.

Untrusted text (issue bodies, comments, CI logs) is rendered into prompts only inside delimited `<untrusted …>` blocks, with the closing delimiter escaped.

### 6.4 Audit

Every GitHub write attempt is recorded via `store/audit_store.py`, whether it is allowed, blocked or dry-run. So are logins, settings and rules changes, the kill switch, run start and end, preflight failures, escalations and skipped issues or PRs; the kinds live in `core/audit.py`. Agents cannot read or write the audit log.

### 6.5 Classifiers

An optional classifier answers fixed multiple-choice questions about a text with a probability per option. It runs on the controller, never in a container, and replaces no agent. Each use case has its own toggle, all off by default; with all off, no classifier is built or called.

- **Injection screen**: the issue text (title, body, trusted comments) of every candidate, and failed CI logs and `check_command` output before they reach the worker. A hit or any failure escalates (fail closed). Nothing is remembered; a false positive is fixed by editing the text.
- **Pre-screen**: ready candidates that are clearly too large or unclear (p ≥ 0.9) escalate before the selector runs. Anything else, including a failure, goes to the selector.
- **Effort routing**: a confident size (p ≥ 0.6) of a new issue sets the worker's effort from `classifier.effort_small/medium/large`; otherwise the worker's own effort is used. Other roles keep theirs.

`pipeline/classify.py` sends one request per issue with only the enabled questions; `domain/classifier_policy.py` holds the questions, validates every answer (exact options, finite probabilities summing to 1) and decides. Each provider module exports a `ClassifierSpec` (name, display name, models, credential, factory); `classifiers/registry.py` checks provider and model on save and in preflight, and gives a provider only its own credential from `SC_AGENT_CREDENTIALS__*`. `agents/registry.py` checks the routed efforts against the worker's backend.

Providers (`jev` is the default): **Jev** (`classifiers/jev.py`, `POST https://api.typesafe.ai/v1/systemone`, `SC_AGENT_CREDENTIALS__TYPESAFE_API_KEY`) and **OpenAI Decisions** (`classifiers/openai_decisions.py`, `POST https://api.openai.com/v1/decisions`, model `gpt-6-luna`, `SC_AGENT_CREDENTIALS__OPENAI_API_KEY`). Adding one: a module with its `ClassifierSpec`, one entry in `classifiers/registry.py`, its credential in `.env.example` and `docs/install.md`.

---

## 7. Modularity contract

Before merging, check:

- [ ] No GitHub call outside `github/gateway.py`
- [ ] No GitHub write without a `domain/` policy check and an audit event
- [ ] No credential passed into a container except the backend's model credential (agents) or the runner's own registration (CI runners); the classifier key stays on the controller
- [ ] No git command run by the controller in an agent-writable directory
- [ ] No I/O in `domain/`
- [ ] Agent output parsed only through `domain/schemas.py`
- [ ] No backend-specific code outside `agents/backends/`, no classifier-specific code outside `classifiers/`
- [ ] Import direction of §3 respected (`uv run --no-sync lint-imports`)
- [ ] New behaviour covered by tests, policy changes covered by adversarial tests

| Wrong | Right |
| --- | --- |
| Prompt says "never add label `skeleton`" as the only defence | `domain/label_policy.py` rejects it; the prompt is just a courtesy |
| `pipeline/` calls `githubkit` directly | `pipeline/` calls `github/gateway.py` |
| Worker container gets `GH_TOKEN` to push | Controller applies the patch in its own clone and pushes |
| `git log` in the agent's workspace on the controller | `git diff` exec'd inside the container, applied in the controller clone |
| `if backend == "claude"` in `pipeline/` | Behaviour behind `AgentBackend` |
| Parse agent output with `json.loads` + dict access | `WorkerResult.model_validate_json` |

---

## 8. When to update this document

In the same change that adds or moves a package, entity, cross-cutting rule or guardrail. Never add changelogs or task notes here.
