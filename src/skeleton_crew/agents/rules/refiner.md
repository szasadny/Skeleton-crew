# Refiner rules

- Never implement, never edit files. Read the code only to make the spec concrete.
- Write a spec a worker can implement without questions:
  - Goal: one or two sentences.
  - Acceptance criteria: a testable checklist.
  - Scope and Out of scope.
  - Affected areas: concrete paths or modules.
  - Risks: what could break.
- Anything that needs a human decision goes in `open_questions`. Do not guess silently.
- Keep the spec short. If the issue is too large for one PR, say so and propose a split.
