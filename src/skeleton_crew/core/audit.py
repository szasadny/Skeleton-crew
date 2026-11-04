from enum import StrEnum


class AuditKind(StrEnum):
    GITHUB_WRITE = "github_write"
    POLICY_VIOLATION = "policy_violation"
    SETTINGS_CHANGED = "settings_changed"
    RULES_CHANGED = "rules_changed"
    KILL_SWITCH = "kill_switch"
    RUN_STARTED = "run_started"
    RUN_FINISHED = "run_finished"
    PREFLIGHT_FAILED = "preflight_failed"
    TASK_ESCALATED = "task_escalated"
    PR_UPDATE_SKIPPED = "pr_update_skipped"
    ISSUE_SKIPPED = "issue_skipped"
    LOGIN = "login"


class AuditOutcome(StrEnum):
    ALLOWED = "allowed"
    BLOCKED = "blocked"
    DRY_RUN = "dry_run"
    INFO = "info"
