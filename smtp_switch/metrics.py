"""Prometheus collectors, exposed at ``GET /metrics`` by the web app.

Gauges that describe "current state" (queue depth, provider health, remaining
headroom) are refreshed by the dispatcher on each poll tick via
:func:`set_provider_gauges` and :func:`set_queue_depth`.
"""

from __future__ import annotations

from prometheus_client import Counter, Gauge, Histogram

MESSAGES_RECEIVED = Counter(
    "smtp_switch_messages_received_total",
    "Messages accepted on the ingress SMTP server.",
    ["account"],
)
MESSAGES_SENT = Counter(
    "smtp_switch_messages_sent_total",
    "Messages successfully relayed to a provider.",
    ["provider"],
)
MESSAGES_FAILED = Counter(
    "smtp_switch_messages_failed_total",
    "Delivery attempts that did not succeed.",
    ["provider", "result"],
)
MESSAGES_DEADLETTERED = Counter(
    "smtp_switch_messages_deadlettered_total",
    "Messages moved to the dead-letter state.",
    ["reason"],
)
DISPATCH_LATENCY = Histogram(
    "smtp_switch_dispatch_latency_seconds",
    "Wall time from claiming a message to a terminal outcome.",
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60),
)
PROVIDER_RELAY_LATENCY = Histogram(
    "smtp_switch_provider_relay_seconds",
    "Time spent in the outbound SMTP conversation with a provider.",
    ["provider"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1, 2, 5, 10, 30),
)

QUEUE_DEPTH = Gauge(
    "smtp_switch_queue_depth",
    "Messages currently in a non-terminal state.",
    ["status"],
)
PROVIDER_HEALTHY = Gauge(
    "smtp_switch_provider_healthy",
    "1 if the provider's circuit breaker is closed, else 0.",
    ["provider"],
)
PROVIDER_HEADROOM = Gauge(
    "smtp_switch_provider_headroom",
    "Remaining sends allowed in a given window before the limit is hit.",
    ["provider", "window"],
)
PROVIDER_INFLIGHT = Gauge(
    "smtp_switch_provider_inflight",
    "Reserved-but-not-released sends against a provider right now.",
    ["provider"],
)


def set_queue_depth(counts: dict[str, int]) -> None:
    for status, value in counts.items():
        QUEUE_DEPTH.labels(status=status).set(value)
