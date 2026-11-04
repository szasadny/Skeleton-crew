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
| 6 | Cross-cutting: config, agent backends, work rules, audit |
| 7 | Modularity contract |
| 8 | When to update this document |

---

## 0. Lookup: where does X go?

Each row has exactly one correct place. Putting it anywhere else violates the contract (§7).

| You are adding / changing… | It goes in… |
| --- | --- |
| Which label changes are allowed | `domain/label_policy.py` |
| Branch naming or the allowed push target | `domain/branch_policy.py` |
| Protected paths, diff size, file-mode rules | `domain/diff_policy.py` |
| Parsing the added lines of a diff | `domain/diff_lines.py` |
| Detecting long code comments in a diff | `domain/comment_policy.py` |
| Task directory key formats (`worker-<id>`, `refine-<id>`, `run-<id>-selector`) | `domain/task_keys.py` |
| Findings, objections and contest matching | `domain/contest_policy.py` |
| A contestable (grey-area) check | A pure function in its policy module, registered in `domain/contestable_checks.py` |
| Sanitising agent text before it reaches GitHub (length cap, @mentions, closing keywords) | `domain/text_policy.py` |
| Deciding whether issue content is trusted (pure rules) | `domain/trust_policy.py` |
| A task, refine or run state, or a transition | `domain/task_state.py` |
| The JSON an agent role must return, and its cross-checks | `domain/schemas.py` (Pydantic, `extra="forbid"`) |
| A UI-editable setting | `domain/settings.py` |
| Any GitHub API call, clone, fetch or push | `github/gateway.py`: the only GitHub entry point |
| Git transport to GitHub (clone, fetch, ls-remote, push) and its auth-header env | `github/_git.py`, used only by the gateway |
| Pure CI evaluation: check states, required checks, job-log formatting | `github/_ci.py`, re-exported by the gateway |
| Fetching the data the trust rules need | `github/trust.py` |
| Ruleset and App-permission checks | `github/preflight.py` |
| The policy check → audit → dry-run sequence every GitHub write runs | `github/write_guard.py` (shared by the gateway and its test fake) |
| Starting, exec'ing, stopping containers; runtime health | `runtime/containers.py` |
| Controller-side git work: worker copies, applying patches, diff summaries | `runtime/workspace.py` |
| Egress allowlist rendering and reload | `runtime/egress.py` |
| Secret scanning of a patch (gitleaks) | `runtime/secret_scan.py` |
| The agent backend interface and the session runner | `agents/base.py`, `agents/session.py` |
| A specific agent CLI (argv, env, output parsing) | `agents/backends/<name>.py`, registered in `agents/registry.py` |
| A role prompt template | `agents/prompts/<role>.md.j2` |
| A default work rule | `agents/rules/<role>.md` (seed for the UI-editable copy) |
| Reading or writing settings, rules, runs, tasks | `store/` |
| Orchestration of a run, worker, refine or CI wait | `pipeline/` |
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
VM (Ubuntu 24.04, Docker + Sysbox)
└─ docker compose
   ├─ caddy         TLS → web UI on the LAN IP
   ├─ controller    FastAPI + APScheduler + SQLite; only holder of the GitHub App key; docker.sock
   ├─ egress-proxy  Squid with a domain allowlist; the only internet route for agent containers
   └─ agent-*       spawned per session: runtime=sysbox-runc, internal network, non-root, resource limits
```

The data directory (`SC_DATA_DIR`) is bind-mounted into the controller at the **same path** as on the host, so the controller can hand host paths to Docker for agent mounts.

---

## 3. Packages and import rules

All code lives in `src/skeleton_crew/`.

| Package | Responsibility | Must NOT |
| --- | --- | --- |
| `domain/` | Pure policy, state machine, schemas, settings model | Do I/O; import any other internal package |
| `core/` | Config, DB engine/session, logging with redaction, audit enums, security primitives, the hardened git runner | Hold domain rules |
| `models/` | SQLAlchemy ORM entities | Hold logic |
| `store/` | Async data access for settings, rules, runs, tasks | Hold business rules |
| `github/` | Gateway (allow-listed operations only, **no merge**), trust data, preflight | Write without the `domain/` policy check and an audit event |
| `runtime/` | Containers, controller-side git work, egress, secret scan | Pass any GitHub credential into a container |
| `agents/` | Backend interface, backends, session runner, prompts, default rules | Call GitHub; import `runtime/` (it receives an executor) |
| `pipeline/` | Run controller, worker flow, PR maintenance, refine flow, CI watcher | Define policy of its own |
| `scheduler/` | Cron triggers from settings, manual "run now" | Contain run logic |
| `web/` | FastAPI routes, Jinja2 templates, HTMX partials | Contain business logic |

Import direction (an arrow means "may import"):

```text
cli → web, scheduler → pipeline → github, runtime, agents, store → models → domain
every package → core (core → domain only)
```

`github/`, `runtime/` and `agents/` never import each other. `pipeline/` composes them. import-linter enforces this in CI.

`github/_git.py` and `github/_ci.py` are private to the gateway: only `github/gateway.py` imports them, enforced by import-linter. Other packages import the CI names from `github/gateway.py`.

`web/` reaches the run controller only through the `web/ports.py` `RunControl` protocol and may import `scheduler/` to re-apply the schedule. Its only `agents/` import is `agents.rules.default_rules`, for "reset to default" on the rules page.

---

## 4. Run flow, task lifecycle, code path

### 4.1 Run flow

1. **Preflight**: ruleset on `main` requires a PR plus ≥1 approval, and the App cannot bypass it (without any ruleset only when `allow_unprotected_base_branch` is on); the App lacks `workflows` and `administration` write; Docker, Sysbox, the agent image and the proxy are healthy; each configured backend has its credential; the kill switch is off. Any failure ends the run as `preflight_failed`.
2. **Candidates**: open issues with the labels that pass the trust rules (`docs/labels.md` § Trust). Issues with an open skeleton PR go to PR maintenance instead.
3. **Selector**: orders candidates up to the daily quota. Every candidate it does not pick is escalated as too large or rejected, with a reason.
4. **Worker pool** (≤ `max_parallel_workers`), and in parallel the **refine flow**. PR maintenance jobs (§4.4) take slots before new issues.
5. **Stop** when candidates or quota run out, the window ends, or the kill switch fires.

**Dry run** (default on): every agent session runs for real, but the gateway only audits its writes. Workers skip the CI wait and go straight to review, so one dry run exercises selector, worker, policy checks, reviewer and refiner without touching GitHub.

### 4.2 Worker task states

Source of truth: `domain/task_state.py`.

```text
QUEUED → PREPARING → CODING → POLICY_CHECK → AWAITING_CI → REVIEWING → DONE
                       ↑            │              │            │
                       └────────────┴──────────────┴────────────┘
              policy failed / CI failed / changes requested (until rounds run out)
any non-terminal state → ESCALATED | FAILED | ABORTED
```

While a task is `AWAITING_CI` its container stays alive, but no session runs, so waiting costs no tokens. CI counts as passed only when every GitHub Actions workflow run for the commit has completed and every check the base-branch ruleset requires has reported green; a required check that never appears ends in the CI timeout. A CI failure or a review finding resumes the same agent session. A failed `check_command` is a policy-check failure that counts against `max_ci_rounds`.

### 4.3 From agent work to PR

The controller never runs git inside a directory an agent can write to: a planted `.git/config` or hook would execute on the controller.

1. The gateway clones the repo into `<data>/tasks/<id>/controller/` (credentials via a one-shot header, never stored). This clone is never mounted into a container.
2. `runtime/workspace` makes a plain local copy at `<data>/tasks/<id>/agent/` and mounts it at `/workspace`. If `setup_command` is set, the controller execs it in the worker container before the first session; failure escalates.
3. After a session, the controller execs `check_command` in the container if set, except while resolving a merge conflict; a failure goes back to the worker with the output tail. Repo commands run as the agent user with no extra env, so they never see the model credential. Then it execs `git diff --binary <base>` **inside the container** (external diff and textconv disabled) and reads it as plain bytes.
4. In the controller clone, with hooks and fsmonitor disabled, it applies the patch on `<base>` and creates a commit whose tree is "base + patch" and whose parent is the current branch tip. Commit metadata comes from the controller, not the agent.
5. `diff_policy`, gitleaks and the checks in `domain/contestable_checks.py` run on that commit versus base. A contestable finding is a violation unless the worker contested it in `objections` with a reason, matched on check, path and line; contested findings go to the reviewer, who decides. Hard checks are never contestable. Only then does the gateway push `skeleton/<n>-<slug>` and open or update the PR.

Reviewer, refiner and selector sessions get a fresh read-only copy. Within one worker job the reviewer keeps its container and session: a later review round resumes it, with the copy refreshed in place to the new head and the changes since its last review in `/skeleton-in/since-review.diff`.

The first push of a new branch uses a lease that requires the branch not to exist. If `skeleton/<n>-<slug>` already exists when a worker starts (a PR closed without merging, or a crash before the PR was opened), the worker comments on the issue and continues from that branch: the controller merges the base into it as in §4.4 (a clean merge is pushed as a merge commit, a conflict goes to a worker), then CI and a fresh review follow and the PR is opened. A branch without changes of its own is replaced by a normal worker run, with a lease on its old head.

### 4.4 PR maintenance

`pipeline/pr_maintenance.py` resolves merge conflicts in open skeleton PRs while their issue still has `skeleton`.

1. A PR qualifies when its head is the last commit the controller pushed in a live run (`runs_store.last_pushed_task`) and it is behind the base branch. Any other head means a human took over; the PR is skipped with an audit event.
2. The controller merges the base into the branch in its own clone with `git merge-tree`, so history is kept and nothing is rebased.
3. **Clean merge**: the job stops without writes. A PR that is only behind stays as it is; GitHub merges it fine.
4. **Conflict**: the conflicted tree becomes a local-only commit for the agent copy. The worker's patch on top of it becomes a merge commit with parents (old head, base). It may only touch conflicted files and must leave no conflict markers. Then CI and a fresh review follow, as for a new PR.
5. A job that needs an agent becomes kind `pr_fix` and counts against `conflict_fixes_per_day`; a clean PR costs no quota. Failure escalates the issue as usual.

---

## 5. Data model

SQLite in the data directory, with Alembic migrations applied at startup.

| Entity | Purpose |
| --- | --- |
| `SettingsRow` | One row holding the UI-editable settings as JSON, validated by `domain/settings.py` |
| `RuleRevision` | Append-only history of each role's work rules; the newest revision is active |
| `Run` | One daily or manual run: trigger, `dry_run`, state, timestamps |
| `WorkerTask` | One issue in one run: kind (`issue`, `pr_update`, `pr_fix`), state, branch, PR, base and head SHA, session id, container id, round counters, cost |
| `RefineTask` | One refine in one run: state, session id |
| `AuditEvent` | Append-only: every GitHub write (allowed, blocked or dry-run), settings or rules change, kill switch |

---

## 6. Cross-cutting

### 6.1 Configuration

- **Bootstrap** (`.env`, `core/config.py`, prefix `SC_`): target repo `owner/name`, GitHub App id and key path, agent credentials, UI password hash, session secret, data directory, image and network names. Never editable in the UI and never logged.
- **Runtime** (`domain/settings.py`, edited in the UI): schedule, limits, per-role agent (backend, model, effort, web tools), label names, protected paths, `setup_command`, `check_command`, egress allowlist, resource limits, `dry_run`, kill switch.

### 6.2 Agent backends

`agents/base.py` defines `AgentBackend`. A backend turns a `SessionRequest` (role, prompt, model, effort, limits, schema, rules file, resume id) into an argv plus environment, and turns the CLI output into a `SessionOutcome` (session id, raw structured output, cost, turns, error flags, early-stop reason). It also declares the egress domains and credentials it needs. `agents/registry.py` lists the models each backend accepts; the UI offers and saves only those. `agents/session.py` runs any backend through an injected executor, with a wall-clock timeout and transcript capture. Output is always validated by `domain/schemas.py`, whether or not the backend enforces a schema itself. The target repo's own agent hooks run as configured. When a session ends cleanly without a result, for example because a hook stopped the turn, the backend reports it as `stopped_early` and `pipeline/sessions.py` resumes the same session with a continue prompt, up to `max_continues` times, then fails closed.

MVP backend: **Claude Code** (`agents/backends/claude_code.py`): `claude -p` with `--output-format stream-json`, `--json-schema`, `--resume`, `--max-turns`, `--disallowedTools` for every interactive tool, and `--max-budget-usd` in API-key mode. Auth is `subscription` (`CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`) or `api_key` (`ANTHROPIC_API_KEY`), always with the unmodified CLI. Codex and OpenCode come after the MVP as extra backends. No other package changes.

### 6.3 Work rules and precedence

Each session gets `common.md` + `<role>.md` as its appended system prompt, in this order of authority:

1. Code policy (`domain/`): cannot be overridden.
2. Skeleton work rules: process.
3. Target repo's `CLAUDE.md` / `AGENTS.md`: code standards.

Untrusted text (issue bodies, comments, CI logs) is rendered into prompts only inside delimited `<untrusted …>` blocks, with the closing delimiter escaped.

### 6.4 Audit

Every GitHub write attempt is recorded via `store/audit_store.py`, whether it is allowed, blocked or dry-run. Agents cannot read or write the audit log.

---

## 7. Modularity contract

Before merging, check:

- [ ] No GitHub call outside `github/gateway.py`
- [ ] No GitHub write without a `domain/` policy check and an audit event
- [ ] No credential passed into a container except the backend's model credential
- [ ] No git command run by the controller in an agent-writable directory
- [ ] No I/O in `domain/`
- [ ] Agent output parsed only through `domain/schemas.py`
- [ ] No backend-specific code outside `agents/backends/`
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
