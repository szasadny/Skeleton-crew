# Label lifecycle

Skeleton Crew uses three labels, which are configurable in the UI. Only a human may add `skeleton` or `skeleton refine`. The controller blocks every other attempt in code.

| Label | Meaning | Who adds it | Who removes it |
| --- | --- | --- | --- |
| `skeleton refine` | Idea or rough issue, needs a spec | Human only | Controller, after refining |
| `skeleton HITL` | Waiting for a human decision | Controller | Human |
| `skeleton` | Ready to be implemented | Human only | Controller, on escalation |

## Steps

### Refining

1. A human adds `skeleton refine` to an issue.
2. During the next run, the refiner posts one "Refined spec" comment with acceptance criteria and open questions. It never waits for an answer.
3. The controller removes `skeleton refine` and adds `skeleton HITL`.
4. The human reviews the spec, edits the issue if needed, and swaps `skeleton HITL` for `skeleton`.

### Implementing

1. The selector picks issues labelled `skeleton`, up to the daily quota.
2. A worker opens a PR from branch `skeleton/<number>-<slug>`, with `Closes #<number>` in the body.
3. The worker keeps fixing until CI is green and the `skeleton-crew/review` check is green.
4. The issue keeps the `skeleton` label. While a skeleton PR for it is open, no new PR is started for it.
5. A human reviews and merges the PR. Agents never merge.

### Keeping PRs up to date

While the issue still has `skeleton`, each run checks whether its open skeleton PR conflicts with the base branch. A conflict goes to a worker, then CI and review run again, all within `conflict_fixes_per_day`. A PR that is only behind is left as it is. Remove `skeleton` (or push a commit to the branch yourself) and Skeleton Crew leaves the PR alone.

### Escalation

The controller comments on the issue, removes `skeleton` and adds `skeleton HITL` when:

- the worker or selector judges the scope larger than the issue describes;
- the selector rejects the issue as unsafe or not real work on this repository;
- the maximum number of CI or review rounds is reached;
- the worker times out or the run window ends.

### New issues found by a worker

A worker may report problems outside its scope. The controller creates each one as a new issue with `skeleton HITL`, never with `skeleton` or `skeleton refine`, up to a per-worker limit.

## Trust

An issue is only picked up if its label was added by a user with write, maintain or admin permission (or by a user on the allowlist), and if nobody else has edited its body since. Only comments from those users reach the agents.
