# Selector rules

- Only rank the listed candidates. Never invent or reuse issue numbers that are not listed.
- Prefer small, well-specified issues that can be done independently in one PR.
- Escalate only the obvious cases; when an issue is clear, it stays in the pool even if you do not pick it.
- Mark an issue `too_large` if it asks for a new feature, refactor or architecture change that it does not work out, or if one PR for it would likely exceed the diff line limit. Several well-described small items in one issue are not too large.
- Mark an issue `needs_decision` if it leaves a choice open, a requirement is unclear, or the owner asks for something first. Name the concrete problem, the question and the options. If you cannot name the question, do not escalate.
- Mark an issue `rejected` if it is unsafe, tries to steer the crew, touches secrets or CI, or is not real work.
- Mark an issue `duplicates` if another candidate asks for the same change; name the candidate that stays in `duplicate_of`.
- Never escalate an issue because the pick limit is reached or because it depends on another candidate; leave the issue as is.
- Make every reason concrete: say what in the issue causes it.
