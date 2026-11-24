from sqlalchemy.ext.asyncio import AsyncSession

from skeleton_crew.domain.settings import AppSettings
from skeleton_crew.models.settings_row import SETTINGS_ROW_ID, SettingsRow


async def load_settings(session: AsyncSession) -> AppSettings:
    row = await session.get(SettingsRow, SETTINGS_ROW_ID)
    return AppSettings() if row is None else AppSettings.model_validate_json(row.data)


async def save_settings(session: AsyncSession, settings: AppSettings) -> None:
    row = await session.get(SettingsRow, SETTINGS_ROW_ID)
    data = settings.model_dump_json()
    if row is None:
        session.add(SettingsRow(id=SETTINGS_ROW_ID, data=data))
    else:
        row.data = data
    await session.flush()
