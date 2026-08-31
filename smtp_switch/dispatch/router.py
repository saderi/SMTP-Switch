"""Provider selection: priority order + failover, capacity- and health-aware."""

from __future__ import annotations

from dataclasses import dataclass

from smtp_switch.dispatch.health import CircuitBreaker
from smtp_switch.dispatch.rate_limiter import RateLimiter, Reservation
from smtp_switch.logging_setup import get_logger
from smtp_switch.providers.registry import ProviderRegistry

log = get_logger("dispatch.router")


@dataclass(slots=True)
class RouteDecision:
    provider: str
    reservation: Reservation


class Router:
    def __init__(
        self,
        registry: ProviderRegistry,
        rate_limiter: RateLimiter,
        breaker: CircuitBreaker,
    ) -> None:
        self._registry = registry
        self._rl = rate_limiter
        self._breaker = breaker

    async def select(self, *, exclude: frozenset[str] = frozenset()) -> RouteDecision | None:
        """Return the highest-priority provider that is up and has headroom.

        Reserves capacity on the chosen provider; the caller must later call
        ``rate_limiter.commit`` or ``rate_limiter.release`` for that reservation.
        """
        for cfg in self._registry.enabled_providers():
            name = cfg.name
            if name in exclude:
                continue
            if not self._breaker.is_available(name):
                continue

            reservation = await self._rl.try_reserve(name)
            if reservation is None:
                continue

            # Breaker may be half-open: claim a probe slot, or hand the
            # reservation back if another probe is already in flight.
            if not await self._breaker.begin_probe(name):
                await self._rl.release(reservation)
                continue

            return RouteDecision(provider=name, reservation=reservation)

        return None

    def candidate_names(self) -> list[str]:
        return [p.name for p in self._registry.enabled_providers()]
