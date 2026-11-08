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
2. During the next run, the refiner posts one short "Refined spec" comment: what must happen, acceptance criteria and open questions for the owner. It never waits for an answer.
3. The controller removes `skeleton refine` and adds `skeleton HITL`.
4. The human reviews the spec, edits the issue if needed, and swaps `skeleton HITL` for `skeleton`.

### Implementing

1. The selector picks issues labelled `skeleton`, up to the daily quota. Issues it does not pick keep `skeleton` for a later run.
2. A worker opens a PR from branch `skeleton/<number>-<slug>`, with `Closes #<number>` in the body.
3. The worker keeps fixing until CI is green and the `skeleton-crew/review` check is green.
4. The issue keeps the `skeleton` label. While a skeleton PR for it is open, no new PR is started for it. An issue with more than one open skeleton PR is skipped entirely.
5. If the branch already exists without an open PR, Skeleton Crew continues it only when the controller pushed its head itself. A branch with someone else's commits, or whose PR was closed without merging, escalates instead.
6. A human reviews and merges the PR. Agents never merge.

### Keeping PRs up to date

While the issue still has `skeleton`, each run asks GitHub whether its open skeleton PR merges cleanly. A conflict goes to a worker, then CI and review run again, all within `conflict_fixes_per_day`. A finished PR (its last task approved) that merges cleanly, or whose state GitHub has not computed yet, is left as it is.

A PR whose last task escalated, failed or was aborted is finished in the next run, within the same quota: the base is merged in if it moved, failed CI is started again with an empty `Re-run CI` commit, pending CI is awaited, red CI goes to a worker, and then the reviewer runs. So after an escalation caused by a passing problem (slow CI, a blocked download), fix the cause and swap `skeleton HITL` for `skeleton`. Remove `skeleton` (or push a commit to the branch yourself) and Skeleton Crew leaves the PR alone.

### Escalation

The controller comments on the issue, removes `skeleton` and adds `skeleton HITL` when:

- the selector judges that the issue asks for a feature, refactor or architecture change it does not work out;
- the selector needs an owner decision; the comment names the problem, the question and the options;
- the worker or reviewer judges the scope larger than the issue describes;
- the selector rejects the issue as unsafe or not real work on this repository;
- the worker reports it is blocked, or a worker or reviewer session fails;
- the setup command fails;
- the maximum number of check, CI, policy or review rounds is reached, or CI does not report or finish in time;
- an existing branch has commits Skeleton Crew did not push, or its PR was closed without merging;
- the worker times out or the run window ends;
- with the injection screen on, the issue text or the CI or check output looks like prompt injection, or the classifier gives no valid answer; edit the text to go on;
- with the pre-screen on, the classifier is sure the issue is too large or unclear.

To go on after an escalation, remove `skeleton HITL` and add `skeleton` again. With an open PR, the next run finishes that PR (see above). To start over instead, close the PR, delete the branch `skeleton/<number>-<slug>` and then swap the labels; the next run starts a fresh branch.

### New issues found by a worker

A worker may report problems outside its scope. The controller creates each one as a new issue with `skeleton HITL`, never with `skeleton` or `skeleton refine`, up to a per-worker limit.

## Trust

An issue is only picked up if its label was added by a user with write, maintain or admin permission (or by a user on the allowlist), and if nobody else has edited its body since. Only comments from those users reach the agents.
