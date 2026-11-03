from sqlalchemy.ext.asyncio import AsyncSession

from skeleton_crew.core.audit import AuditKind, AuditOutcome
from skeleton_crew.domain.settings import AppSettings
from skeleton_crew.models.settings_row import SETTINGS_ROW_ID, SettingsRow
from skeleton_crew.store import audit_store


async def load_settings(session: AsyncSession) -> AppSettings:
    row = await session.get(SettingsRow, SETTINGS_ROW_ID)
    return AppSettings() if row is None else AppSettings.model_validate_json(row.data)


def _changed_keys(old: AppSettings, new: AppSettings) -> list[str]:
    before, after = old.model_dump(mode="json"), new.model_dump(mode="json")
    return sorted(key for key in after if before.get(key) != after[key])


async def save_settings(session: AsyncSession, settings: AppSettings) -> None:
    row = await session.get(SettingsRow, SETTINGS_ROW_ID)
    old = AppSettings() if row is None else AppSettings.model_validate_json(row.data)
    if row is None:
        row = SettingsRow(id=SETTINGS_ROW_ID, data=settings.model_dump_json())
        session.add(row)
    else:
        row.data = settings.model_dump_json()
    changed = _changed_keys(old, settings)
    if changed:
        # Key names only: values may be sensitive and the audit log is shown in the UI.
        await audit_store.record(
            session,
            AuditKind.SETTINGS_CHANGED,
            AuditOutcome.INFO,
            f"settings changed: {', '.join(changed)}",
            {"keys": changed},
        )
    await session.flush()
