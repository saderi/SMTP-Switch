"""Effective provider list = static config + operator overrides from the DB.

The dashboard can disable/enable a provider without a redeploy; that toggle is
stored in ``provider_overrides`` and layered on top of the YAML config here.
"""

from __future__ import annotations

from sqlalchemy import select

from smtp_switch.config import ProviderConfig, Settings
from smtp_switch.db.models import ProviderOverride
from smtp_switch.db.session import session_scope
from smtp_switch.logging_setup import get_logger

log = get_logger("providers.registry")


class ProviderRegistry:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._by_name: dict[str, ProviderConfig] = {p.name: p for p in settings.providers}
        self._overrides: dict[str, bool | None] = {}

    async def refresh_overrides(self) -> None:
        async with session_scope() as session:
            rows = (await session.execute(select(ProviderOverride))).scalars().all()
            self._overrides = {r.provider: r.enabled for r in rows}

    async def set_enabled(self, name: str, enabled: bool | None, note: str | None = None) -> None:
        if name not in self._by_name:
            raise KeyError(name)
        async with session_scope() as session:
            row = await session.get(ProviderOverride, name)
            if row is None:
                row = ProviderOverride(provider=name)
                session.add(row)
            row.enabled = enabled
            row.note = note
        self._overrides[name] = enabled
        log.info("provider_override_set", provider=name, enabled=enabled)

    def all(self) -> list[ProviderConfig]:
        return list(self._by_name.values())

    def get(self, name: str) -> ProviderConfig | None:
        return self._by_name.get(name)

    def is_enabled(self, name: str) -> bool:
        cfg = self._by_name.get(name)
        if cfg is None:
            return False
        override = self._overrides.get(name)
        return cfg.enabled if override is None else override

    def enabled_providers(self) -> list[ProviderConfig]:
        return sorted(
            (p for p in self._by_name.values() if self.is_enabled(p.name)),
            key=lambda p: (p.priority, p.name),
        )
