# How a run works

What Skeleton Crew does during a run, for operators and contributors. Where the code lives is in [ARCHITECTURE.md](../ARCHITECTURE.md); the guardrails are in [SECURITY.md](../SECURITY.md); the labels are in [labels.md](labels.md).

## Run flow

1. **Preflight**: a ruleset on the base branch requires a PR plus ≥1 approval, and the App cannot bypass it (without any ruleset only when `allow_unprotected_base_branch` is on); the App has the required permissions, no `workflows` permission and no `administration` write; the agent settings pass `agents/registry.py`; Docker, Sysbox, the agent image and the proxy are healthy; each configured backend has its credential; with self-hosted CI on, the repo is private, the runner image, network and proxy are healthy and at least one runner is registered; the required checks can be read, the egress allowlist applied and the repo docs read (see Work rules) from a clone of the base branch that the selector reuses. Any failure ends the run as `preflight_failed`. While the kill switch is on, no run starts.
2. **Candidates**: open issues with the labels that pass the trust rules (`docs/labels.md` § Trust), except issues that also carry `skeleton HITL` (or, for `skeleton`, `skeleton refine`). Issues with an open skeleton PR go to PR maintenance instead. Expired task directories are pruned before this step . With a classifier use case on, each candidate is screened first (see Classifiers).
3. **Selector**: orders candidates up to the daily quota; an `aborted` task does not count towards it. It escalates only issues that are clearly too large (unspecified, or a diff likely over `max_diff_lines`), need an owner decision (with the question and options), are rejected or duplicate another candidate; every other candidate it does not pick stays queued.
4. **Worker pool** (≤ `max_parallel_workers`), and in parallel the **refine flow**. PR maintenance jobs (see PR maintenance) take slots before new issues.
5. **Stop** when candidates or quota run out, the window ends, the kill switch fires, or an agent session hits the backend's usage limit (`SessionOutcome.usage_limited`): that task ends `aborted` without escalating, and the run ends `failed`.

**Dry run** (default on): every agent session runs for real, but the gateway only audits its writes. Workers skip the CI wait and go straight to review, so one dry run exercises selector, worker, policy checks, reviewer and refiner without touching GitHub.

## Worker task states

Source of truth: `domain/task_state.py`.

```text
        STARTED     PREPARED  CODE_DONE      POLICY_PASSED  CI_PASSED   REVIEW_APPROVED
QUEUED ───────→ PREPARING ──→ CODING ──→ POLICY_CHECK ──→ AWAITING_CI ──→ REVIEWING ──→ DONE
                                ↑            │                  │              │
                                └────────────┴──────────────────┴──────────────┘
                 POLICY_FAILED / CHECK_FAILED, CI_FAILED, REVIEW_CHANGES (until rounds run out)
any non-terminal state → ESCALATED (ESCALATE) | FAILED (ERROR) | ABORTED (ABORT)
```

While a task is `AWAITING_CI` its container stays alive, but no session runs, so waiting costs no tokens. With self-hosted CI on, this state starts the runners (see Self-hosted CI runners), as does queued CI on a skeleton branch outside a run. CI counts as passed only when every GitHub Actions workflow run for the commit has completed and every check the base-branch ruleset requires has reported green; a required check that never appears ends in the CI timeout. If nothing reports within `ci_grace_minutes`, the task escalates when `require_ci` is on and goes to review otherwise. A CI failure or a review finding resumes the same agent session. Each kind of round has its own limit: a failed `check_command` fires `CHECK_FAILED` and counts against `max_check_rounds`, red CI against `max_ci_rounds`, a policy violation (`POLICY_FAILED`) against `max_policy_rounds` and a reviewer rejection against `max_review_rounds`. When the worker's time runs out while it waits for CI, the escalation says so. A clean merge of the base into a continued branch has no coding step: it goes from `CODING` straight to `POLICY_CHECK`.

## From agent work to PR

The controller never runs git inside a directory an agent can write to: a planted `.git/config` or hook would execute on the controller.

1. The gateway clones the repo into `<data>/tasks/<key>/controller/` (credentials via a one-shot header, never stored). This clone is never mounted into a container.
2. `runtime/controller_git` makes a plain local copy at `<data>/tasks/<key>/agent/` and mounts it at `/workspace`. If `setup_command` is set, the controller execs it in the worker container before the first session; failure escalates.
3. After a session, the controller execs `check_command` in the container if set, except while resolving a merge conflict; a failure goes back to the worker with the output tail. Repo commands run as the agent user with no extra env, so they never see the model credential. Then it execs `git diff --binary <base>` **inside the container** (external diff and textconv disabled) and reads it as plain bytes.
4. In the controller clone, with hooks and fsmonitor disabled, it applies the patch on `<base>` and creates a commit whose tree is "base + patch" and whose parent is the current branch tip. Commit metadata comes from the controller, not the agent.
5. `diff_policy`, gitleaks and the checks in `domain/contest_policy.py` run on that commit versus base. A contestable finding is a violation unless the worker contested it in `objections` with a reason, matched on check, path and line; contested findings go to the reviewer, who decides. Hard checks are never contestable. Only then does the gateway push `skeleton/<n>-<slug>` and open or update the PR.

Reviewer, refiner and selector sessions get a fresh read-only copy. Within one worker job the reviewer keeps its container and session: a later review round resumes it, with the copy refreshed in place to the new head and the changes since its last review in `/skeleton-in/since-review.diff`. Every review must list in `missing_doc_updates` each doc that should have changed but did not (an empty list means none); `domain/schemas.py` rejects `approve` while that list is non-empty, so a missing doc update always goes back to the worker.

Workers and reviewers must list in `blockers` the sandbox or access problems that hindered the work (missing tool, blocked domain, missing access), also when worked around. Checks they did not run are not blockers: CI runs them. Workers list those in `unverified`, which the controller validates and discards. Reviewers trust CI and run no checks themselves. The task row keeps them all, the PR body lists the worker's blockers known when the PR opens, and each review check lists the round's worker and reviewer blockers.

The first push of a new branch uses a lease that requires the branch not to exist. Each run lists open PRs once (`open_skeleton_prs`); an issue with more than one open skeleton PR is skipped for new work and maintenance, with an audit event. If `skeleton/<n>-<slug>` already exists without an open PR when a worker starts, the controller decides before any write (`domain/branch_policy.continuation_problem`):

- The head is not the last commit the controller pushed in a live run (`runs_store.last_pushed_task`): escalate ("the branch has commits Skeleton Crew did not push"). Foreign content is never adopted, so it never skips the per-push secret scan.
- The branch has a PR that was closed without merging: escalate. A closed PR is never resurrected.
- The branch has no changes of its own (its merge with the base equals the base, e.g. after a merged PR whose branch was kept): it is replaced by a normal worker run, with a lease on its old head.
- Otherwise (a crash before the PR was opened) the worker comments on the issue and continues from that branch: the controller merges the base into it as in PR maintenance (a clean merge is pushed as a merge commit, a conflict goes to a worker), then CI and a fresh review follow and the PR is opened.

## PR maintenance

`pipeline/pr_maintenance.py` resolves merge conflicts in open skeleton PRs while their issue still has `skeleton`, and finishes PRs whose last task did not end `done` (escalated, failed or aborted), e.g. after a human put `skeleton` back. The merge itself lives in `pipeline/branch_update.py`, shared with continued branches (see From agent work to PR); the job loop after it is `pipeline/job_engine.py`. PR maintenance runs even when the selector fails.

1. A PR qualifies only when its head is the last commit the controller pushed in a live run (`runs_store.last_pushed_task`). Any other head means a human took over; the PR is skipped with an audit event.
2. While fix quota is left, an unfinished PR gets a `pr_fix` task right away. For a finished PR the controller reads GitHub's `mergeable` flag. `true`: skipped with an audit event, no task and no clone; a PR that is only behind stays as it is. `null` (GitHub still computing): polled a few times, then skipped with an audit event. `false`: a `pr_fix` task is created. Each task takes one `conflict_fixes_per_day`, unless it ends `aborted`.
3. The controller merges the base into the branch in its own clone with `git merge-tree`, so history is kept and nothing is rebased.
4. **Conflict**: the conflicted tree becomes a local-only commit for the agent copy. The worker's patch on top of it becomes a merge commit with parents (old head, base). It may only touch conflicted files and must leave no conflict markers. Then CI and a fresh review follow, as for a new PR. Failure escalates the issue as usual.
5. **Clean, finished PR**: if the local merge turns out clean after all (a race with GitHub), the task ends `aborted` without writes or agent, so it takes no quota.
6. **Clean, unfinished PR**: as for a continued branch (see From agent work to PR), a merge that changes the tree is pushed as a merge commit. Otherwise, if CI on the head failed, the controller pushes an empty commit (`Re-run CI`) to start CI again, so the App needs no write access to Actions; pending CI is awaited and green CI goes straight to review. Red CI after that goes to a worker as a CI round.

## Self-hosted CI runners

Self-hosted CI (`pipeline/runner_pool.py`) reconciles every few seconds. Runners run only when `ci.enabled` is on, the kill switch is off, the repo is private and there is demand: a worker task of a live (not dry) run is `AWAITING_CI`, or, when none is, GitHub reports a queued or in-progress workflow run on a `skeleton/` branch (`skeleton_ci_active`, cached for `CI_DEMAND_TTL_S`; a failed read counts as no demand). Then each runner with a registration in `<data>/runners/runner-<n>/` gets a container that runs one job (`run.sh --once`) and exits, and the pool recreates it from the image. When the demand ends, a runner that is busy with a job keeps running until the job ends (`busy`, "finishing its job") and the rest are removed; then the pool shows `standby`. CI off, the kill switch or a public repo removes every runner at once and shows `offline` with the reason. The registration is mounted read-only and copied in, so a job cannot change what the next job starts from. Before every job, the image's job-started hook fails any job whose branch does not start with `domain/branch_policy.BRANCH_PREFIX` (passed in as `SC_BRANCH_PREFIX`), so the runners only run Skeleton Crew's own pull requests. Runners are registered once by hand (`docs/install.md`); the App never creates registration tokens.

## Agent backends

`agents/base.py` defines `AgentBackend`. A backend turns a `SessionRequest` (role, prompt and the inbox file that holds it, model, effort, limits, schema, rules file, resume id) into an argv plus environment; the prompt is read from that file, never passed as an argument, because Linux caps one argument at 128 KiB, and turns the CLI output into a `SessionOutcome` (session id, raw structured output, cost, turns, error flags, early-stop reason). It also declares the egress domains and credentials it needs, and the repo docs its CLI loads by itself (`auto_loaded_docs`, e.g. `CLAUDE.md` and `.claude/CLAUDE.md` for Claude Code). Each backend module exports a `BackendSpec` (name, display name, models, efforts, options with allowed values and defaults, credential names, web-tools support, factory). `agents/registry.py` holds the specs: it builds a backend with only its declared credentials and its options, and rejects an unknown backend, model, effort, option or unsupported web tools, on UI save (`field_problems`) and in preflight (`settings_problems`; a run then ends `preflight_failed`). `agents/session.py` runs any backend through an injected executor with transcript capture; the executor (`runtime/containers.py`) enforces the wall-clock timeout with SIGINT and a hard kill, so backends add no timeout wrapper of their own. Output is always validated by `domain/schemas.py`, whether or not the backend enforces a schema itself. The target repo's own agent hooks run as configured. When a session ends cleanly without a result, for example because a hook stopped the turn, the backend reports it as `stopped_early` and `pipeline/sessions.py` resumes the same session with a continue prompt, up to `max_continues` times, then fails closed. A continue only gets what is left of the session's `max_turns`, `session_timeout_minutes` and budget, and none is started once one of them is used up.

Backends (`claude_code` is the default):

- **Claude Code** (`agents/backends/claude_code.py`): `claude -p` with `--output-format stream-json`, `--json-schema`, `--resume`, `--max-turns`, `--disallowedTools` for every interactive tool, and `--max-budget-usd` in API-key mode. Option `auth_mode` is `subscription` (`SC_AGENT_CREDENTIALS__CLAUDE_OAUTH_TOKEN` from `claude setup-token`, passed as `CLAUDE_CODE_OAUTH_TOKEN`) or `api_key` (`SC_AGENT_CREDENTIALS__ANTHROPIC_API_KEY`, passed as `ANTHROPIC_API_KEY`), always with the unmodified CLI.
- **Codex** (`agents/backends/codex.py`): `codex exec --json` (`exec resume <id>` to resume), `--dangerously-bypass-approvals-and-sandbox`, `-c web_search="disabled"` unless web tools are on. It has no turn limit, so the session timeout is its only bound, and its strict schema mode rejects optional fields, so it gets no CLI schema. Option `auth_mode` is `api_key` (`OPENAI_API_KEY`, passed as `CODEX_API_KEY`) or `subscription` (`CODEX_AUTH_JSON`, written to `~/.codex/auth.json`).
- **Gemini CLI** (`agents/backends/gemini.py`): headless `gemini` with `--output-format stream-json`, `--approval-mode yolo`, `--skip-trust`, `--resume <id>`, and `--allowed-mcp-server-names` with a name no server has, so repo MCP servers stay off. The turn limit (`maxSessionTurns`), excluded tools (interactive always, web unless enabled) and auth type are written to `~/.gemini/settings.json` before each session. Effort is `default` only. Option `auth_mode` is `api_key` (`GEMINI_API_KEY`) or `subscription` (`GEMINI_OAUTH_CREDS_JSON`, written to `~/.gemini/oauth_creds.json`).
- **OpenCode** (`agents/backends/opencode.py`): `opencode run --format json` with `-m <provider>/<model>`, `--variant` for effort and `--session` to resume. A per-session `OPENCODE_CONFIG_CONTENT` denies the `question` tool and, unless web tools are on, `webfetch`/`websearch`, loads the rules file as `instructions` and sets the agent's `steps`, which OpenCode treats as advisory. Option `provider` (`anthropic`, `openai`, `openrouter`, `google`) picks one key and one egress host, and limits the models to that provider's prefix (`BackendSpec.model_filter`). Option `auth_mode` is `api_key` (that provider's key) or `subscription` (`OPENCODE_AUTH_JSON`, a ChatGPT sign-in written to `~/.local/share/opencode/auth.json`), which works only with provider `openai` and uses `chatgpt.com` and `auth.openai.com` as egress hosts. Catalog fetches, auto-update and CLAUDE.md compatibility are off in the image.

Every backend builds its argv with `agents/backends/_shared.py`: the prompt (and, for CLIs without a system-prompt file option, the rules before it) goes to stdin, a subscription login file is written from `SC_AUTH_FILE` only when absent (the CLI may have refreshed it), and a per-session settings file is rewritten each time.

Adding a backend:

1. A module `agents/backends/<name>.py` implementing `AgentBackend` and exporting its `BackendSpec`.
2. One entry for that spec in `agents/registry.py`.
3. The CLI install in `runtime/images/agent/Dockerfile`, pinned, with a checksum where the CLI ships release binaries.
4. Its credentials as `SC_AGENT_CREDENTIALS__<NAME>` in `.env`, documented in `.env.example` and `docs/install.md`.

No other package changes: the UI, preflight and pipeline read everything from the registry.

## Work rules

Each session gets `common.md` + `<role>.md` + the repo docs as its appended system prompt, in this order of authority:

1. Code policy (`domain/`): cannot be overridden.
2. Skeleton work rules: process.
3. Target repo's docs: code standards.

The repo docs are read once per run from the base branch (`domain/repo_docs.py`): every tracked `CLAUDE.md` and `AGENTS.md`, `.claude/CLAUDE.md`, and the tracked `.md` files their relative links point to, one level deep. Docs the role's backend already loads itself are left out, but their links still count. Each doc is fenced, and docs over the size or count caps are listed by path instead.

Untrusted text (issue bodies, comments, CI logs) is rendered into prompts only inside delimited `<untrusted …>` blocks, with the closing delimiter escaped.

## Classifiers

An optional classifier answers fixed multiple-choice questions about a text with a probability per option. It runs on the controller, never in a container, and replaces no agent. Each use case has its own toggle, all off by default; with all off, no classifier is built or called.

- **Injection screen**: the issue text (title, body, trusted comments) of every candidate, and failed CI logs and `check_command` output before they reach the worker. A hit or any failure escalates (fail closed). Nothing is remembered; a false positive is fixed by editing the text.
- **Pre-screen**: ready candidates that are clearly too large or unclear (p ≥ 0.9) escalate before the selector runs. Anything else, including a failure, goes to the selector.
- **Effort routing**: a confident size (p ≥ 0.6) of a new issue sets the worker's effort from `classifier.effort_small/medium/large`; otherwise the worker's own effort is used. Other roles keep theirs.

`pipeline/classify.py` sends one request per issue with only the enabled questions; `domain/classifier_policy.py` holds the questions, validates every answer (exact options, finite probabilities summing to 1) and decides. Each provider module exports a `ClassifierSpec` (name, display name, models, credential, factory); `classifiers/registry.py` checks provider and model on save and in preflight, and gives a provider only its own credential from `SC_AGENT_CREDENTIALS__*`. `agents/registry.py` checks the routed efforts against the worker's backend.

Providers (`jev` is the default): **Jev** (`classifiers/jev.py`, `POST https://api.typesafe.ai/v1/systemone`, `SC_AGENT_CREDENTIALS__TYPESAFE_API_KEY`) and **OpenAI Decisions** (`classifiers/openai_decisions.py`, `POST https://api.openai.com/v1/decisions`, model `gpt-6-luna`, `SC_AGENT_CREDENTIALS__OPENAI_API_KEY`). Adding one: a module with its `ClassifierSpec`, one entry in `classifiers/registry.py`, its credential in `.env.example` and `docs/install.md`.
