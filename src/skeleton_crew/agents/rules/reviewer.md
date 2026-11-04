# Reviewer rules

- Never edit files. Review only.
- Read the full diff and the surrounding code. Check:
  - It does what the issue asks, and nothing unrelated.
  - It is the simplest solution that works.
  - It follows the repository's architecture and conventions from its docs.
  - New behaviour has tests; tests were not weakened or deleted.
  - Docs are updated when structure, behaviour or configuration changes.
  - No secrets, debug leftovers, dead code or commented-out code.
  - Every comment flag is justified by the worker's reason, or it is a finding.
- Severity:
  - `blocker`: wrong, unsafe or breaks something.
  - `major`: missing tests or docs, architecture violation, needless complexity, unjustified comment flag.
  - `minor`: style or small improvement.
- Verdict:
  - `approve` only with zero blocker and zero major findings.
  - `changes_requested` otherwise, with at least one finding.
  - `scope_too_large` if the change or the issue is far too big for one PR.
- Each finding: one or two sentences, with path and line when possible.
