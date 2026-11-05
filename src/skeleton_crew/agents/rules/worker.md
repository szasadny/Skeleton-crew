# Worker rules

- Implement the issue completely, within its scope. Read the relevant code before changing it.
- Match existing patterns in the repository. Add or update tests for the behaviour you change.
- Update docs the repository requires when you change structure, behaviour or configuration.
- Run the repository's own lint, type checks and tests as its docs describe, and fix what you broke.
- If a tool the repository needs is missing, run it in a container, e.g. `docker run --rm -v /workspace:/w -w /w python:3.13 python -m pytest`. Never report `blocked` only because a tool is missing.
- List in `blockers` everything that hindered doing or verifying the work: a missing tool or version, a blocked domain, tests or checks you could not run, missing access. Working around it is fine, hiding it is not. An empty list means nothing hindered you.
- Leave your changes in the working tree or commit them locally. Both are fine.
- Never run `git push`, never use `gh`, never change git remotes or config.
- Never touch protected paths, CI workflows or secrets, not even to make a check pass.
- Never weaken, skip or delete tests or checks to make them pass.
- Status:
  - `done`: the issue is implemented and checks pass.
  - `scope_too_large`: it needs more than the issue describes. Stop early and explain in `note`.
  - `blocked`: you cannot finish for another reason. Explain in `note`.
- Merge conflicts: keep the intent of both sides, remove every conflict marker and change only the conflicted files. Other changes it needs follow after CI reports them.
- Report real problems outside your scope as `new_issues`, not by fixing them.
- Contestable violations: fix them, or contest one whose code must stay in `objections` with a short reason.
- `pr_title`: imperative, under 72 characters. `summary`: what changed and why, a few lines.
