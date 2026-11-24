from collections.abc import Awaitable, Callable, Iterable, Mapping

from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from skeleton_crew.core.audit import AuditEntry, AuditKind, AuditOutcome
from skeleton_crew.domain.settings import AppSettings, with_updates
from skeleton_crew.services.errors import GENERAL, InvalidInput
from skeleton_crew.store import audit_store, settings_store

type Problems = Mapping[str, list[str]]
type Check = Callable[[AppSettings], Problems]
type OnSaved = Callable[[AppSettings], None]


class InvalidSettings(InvalidInput):
    pass


def _by_field(problems: Iterable[tuple[str, str]], known: Iterable[str]) -> dict[str, list[str]]:
    names = set(known)
    errors: dict[str, list[str]] = {}
    for location, message in problems:
        parts = location.split(".")
        prefixes = (".".join(parts[:i]) for i in range(len(parts), 0, -1))
        target = next((p for p in prefixes if p in names), GENERAL)
        errors.setdefault(target, []).append(message)
    return errors


def _validation_problems(exc: ValidationError) -> list[tuple[str, str]]:
    return [
        (".".join(str(part) for part in error["loc"]), error["msg"].removeprefix("Value error, "))
        for error in exc.errors()
    ]


def _changed_keys(old: AppSettings, new: AppSettings) -> list[str]:
    before, after = old.model_dump(mode="json"), new.model_dump(mode="json")
    return sorted(key for key in after if before.get(key) != after[key])


async def _save(session: AsyncSession, old: AppSettings, new: AppSettings) -> None:
    await settings_store.save_settings(session, new)
    changed = _changed_keys(old, new)
    if changed:
        # Key names only: values may be sensitive and the audit log is shown in the UI.
        await audit_store.record(
            session,
            AuditEntry(
                AuditKind.SETTINGS_CHANGED,
                AuditOutcome.INFO,
                f"settings changed: {', '.join(changed)}",
                {"keys": changed},
            ),
        )


def _checked(
    current: AppSettings, updates: Mapping[str, object], check: Check | None
) -> AppSettings:
    try:
        new = with_updates(current, updates)
    except ValidationError as exc:
        raise InvalidSettings(_by_field(_validation_problems(exc), updates)) from None
    problems = check(new) if check is not None else {}
    found = [(name, message) for name, messages in problems.items() for message in messages]
    if found:
        raise InvalidSettings(_by_field(found, updates))
    return new


async def update_settings(
    session: AsyncSession,
    updates: Mapping[str, object],
    check: Check | None = None,
    on_saved: OnSaved | None = None,
) -> AppSettings:
    current = await settings_store.load_settings(session)
    new = _checked(current, updates, check)
    await _save(session, current, new)
    await session.commit()
    if on_saved is not None:
        on_saved(new)
    return new


async def set_flags(
    session: AsyncSession, *, dry_run: bool | None = None, kill_switch: bool | None = None
) -> AppSettings:
    flags = {"dry_run": dry_run, "kill_switch": kill_switch}
    current = await settings_store.load_settings(session)
    new = current.model_copy(update={name: on for name, on in flags.items() if on is not None})
    await _save(session, current, new)
    if kill_switch is not None:
        summary = "kill switch engaged from the UI" if kill_switch else "kill switch released"
        await audit_store.record(
            session, AuditEntry(AuditKind.KILL_SWITCH, AuditOutcome.INFO, summary)
        )
    await session.commit()
    return new


async def engage_kill_switch(session: AsyncSession, stop: Callable[[], Awaitable[None]]) -> None:
    await set_flags(session, kill_switch=True)
    await stop()
