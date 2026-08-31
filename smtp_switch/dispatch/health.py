"""Per-provider circuit breaker.

States:

* **closed**  — normal; failures are counted, ``failure_threshold`` in a row opens it.
* **open**    — all traffic skipped until ``cooldown_seconds`` elapse.
* **half-open** — after the cooldown, up to ``half_open_max_probes`` messages are
  allowed through. One success closes the breaker; one failure re-opens it.

State is cached in memory for fast routing decisions and written through to the
``provider_state`` table so a restart does not forget an ongoing outage.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select

from smtp_switch.config import CircuitBreakerConfig
from smtp_switch.db.models import ProviderState
from smtp_switch.db.session import session_scope
from smtp_switch.logging_setup import get_logger
from smtp_switch.util import utcnow

log = get_logger("dispatch.health")


@dataclass
class _State:
    healthy: bool = True
    consecutive_failures: int = 0
    opened_at: datetime | None = None
    half_open_probes: int = 0
    last_failure_at: datetime | None = None
    last_success_at: datetime | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class CircuitBreaker:
    def __init__(self, cfg: CircuitBreakerConfig) -> None:
        self._cfg = cfg
        self._states: dict[str, _State] = {}

    def _st(self, provider: str) -> _State:
        return self._states.setdefault(provider, _State())

    async def load(self) -> None:
        async with session_scope() as session:
            rows = (await session.execute(select(ProviderState))).scalars().all()
            for r in rows:
                self._states[r.provider] = _State(
                    healthy=r.healthy,
                    consecutive_failures=r.consecutive_failures,
                    opened_at=r.opened_at,
                    half_open_probes=r.half_open_probes,
                    last_failure_at=r.last_failure_at,
                    last_success_at=r.last_success_at,
                )
        log.debug("circuit_breaker_loaded", providers=list(self._states))

    # ---------------------------------------------------------------- queries
    def status(self, provider: str) -> str:
        st = self._st(provider)
        if st.healthy:
            return "closed"
        if st.opened_at is None:
            return "open"
        elapsed = (utcnow() - st.opened_at).total_seconds()
        return "half_open" if elapsed >= self._cfg.cooldown_seconds else "open"

    def is_available(self, provider: str) -> bool:
        """Cheap check used by the router before it even tries to reserve."""
        state = self.status(provider)
        if state == "closed":
            return True
        if state == "open":
            return False
        return self._st(provider).half_open_probes < self._cfg.half_open_max_probes

    async def begin_probe(self, provider: str) -> bool:
        """Claim a half-open probe slot (no-op when the breaker is closed)."""
        st = self._st(provider)
        async with st.lock:
            state = self.status(provider)
            if state == "closed":
                return True
            if state == "open":
                return False
            if st.half_open_probes >= self._cfg.half_open_max_probes:
                return False
            st.half_open_probes += 1
            await self._persist(provider)
            return True

    # ---------------------------------------------------------------- updates
    async def record_success(self, provider: str) -> None:
        st = self._st(provider)
        async with st.lock:
            was_down = not st.healthy
            st.healthy = True
            st.consecutive_failures = 0
            st.opened_at = None
            st.half_open_probes = 0
            st.last_success_at = utcnow()
            await self._persist(provider)
        if was_down:
            log.info("circuit_closed", provider=provider)

    async def record_failure(self, provider: str, *, error: str | None = None) -> None:
        st = self._st(provider)
        async with st.lock:
            st.consecutive_failures += 1
            st.last_failure_at = utcnow()
            just_opened = False
            if self.status(provider) == "half_open":
                # A probe failed: straight back to open, restart the cooldown.
                st.healthy = False
                st.opened_at = utcnow()
                st.half_open_probes = 0
                just_opened = True
            elif st.healthy and st.consecutive_failures >= self._cfg.failure_threshold:
                st.healthy = False
                st.opened_at = utcnow()
                st.half_open_probes = 0
                just_opened = True
            await self._persist(provider)
        if just_opened:
            log.warning(
                "circuit_opened",
                provider=provider,
                failures=st.consecutive_failures,
                cooldown_s=self._cfg.cooldown_seconds,
                error=error,
            )

    async def _persist(self, provider: str) -> None:
        st = self._states[provider]
        async with session_scope() as session:
            row = await session.get(ProviderState, provider)
            if row is None:
                row = ProviderState(provider=provider)
                session.add(row)
            row.healthy = st.healthy
            row.consecutive_failures = st.consecutive_failures
            row.opened_at = st.opened_at
            row.half_open_probes = st.half_open_probes
            row.last_failure_at = st.last_failure_at
            row.last_success_at = st.last_success_at
            row.updated_at = utcnow()

    def snapshot(self) -> dict[str, dict]:
        out: dict[str, dict] = {}
        for name in self._states:
            st = self._st(name)
            out[name] = {
                "status": self.status(name),
                "healthy": st.healthy,
                "consecutive_failures": st.consecutive_failures,
                "opened_at": st.opened_at,
                "last_failure_at": st.last_failure_at,
                "last_success_at": st.last_success_at,
            }
        return out
