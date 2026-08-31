"""Shared handle to the running subsystems, passed to the web layer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from smtp_switch.config import Settings
    from smtp_switch.dispatch.health import CircuitBreaker
    from smtp_switch.dispatch.worker import Dispatcher
    from smtp_switch.ingress.server import IngressServer
    from smtp_switch.providers.registry import ProviderRegistry


@dataclass
class RuntimeContext:
    settings: Settings
    registry: ProviderRegistry
    breaker: CircuitBreaker
    dispatcher: Dispatcher
    ingress: IngressServer | None = None

    @property
    def rate_limiter(self):
        return self.dispatcher.rate_limiter
